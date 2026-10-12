"""团建活动策划API路由"""

import asyncio
import re
import uuid
from datetime import date as _date
from pathlib import Path
from typing import List, Optional

import aiosqlite
from fastapi import APIRouter, HTTPException
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore
from langgraph.types import Command
from pydantic import BaseModel, Field, field_validator, model_validator

from ...models.schemas import (
    EventPlan, EventRequest, PersonPref, PlaceInput, Proposal,
)
from ...graph import build_event_graph
from ...graph.errors import AppError
from ...graph.nodes import KEY_FIELDS, reassess_risk
from ...graph.ttl_cache import all_stats as cache_stats
from ..errors import to_http_error

router = APIRouter(prefix="/event", tags=["团建活动策划"])

# checkpoint 数据库(backend/data/checkpoints.db), HITL 状态持久化
_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "checkpoints.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 团队记忆数据库(backend/data/memory.db), Store 持久化(namespace=("team", team_id))
_STORE_PATH = Path(__file__).resolve().parents[3] / "data" / "memory.db"

# 图实例与 saver/store(单例)
_event_graph = None
_saver: Optional[AsyncSqliteSaver] = None
_store: Optional[AsyncSqliteStore] = None

# 回环+HITL 重走图的递归上限(审批否决重走一轮约 12 个超步, 留足余量)
_RECURSION_LIMIT = 40

# 僵尸 thread(提交后从未回来审批)的保留天数: 超期则启动时按 uuid6 时间戳清理
_CHECKPOINT_TTL_DAYS = 7

# 服务端签发的 team_id 形态: `team_` + 32 位小写 hex(见 _resolve_team_id)
_TEAM_ID_RE = re.compile(r"^team_[0-9a-f]{32}$")


def _resolve_team_id(raw: Optional[str]) -> str:
    """把请求里的 team_id 归一成 namespace 键: 只认本服务签发过的形态

    演进过三代, 每代都在补同一个洞:
    1. `team_id or "default"`: 所有不传的人共用一个桶, 团队画像/复盘互相注入;
    2. 用户传什么就存什么: 隔离是有了, 但**没有鉴权** —— 知道(或猜到)别人
       team_id 就能读到对方的团队画像与复盘, 而 parse_request 会把历史注入
       解析 prompt, 等于直接把别人的记忆读出来; 也能往里写脏数据。
       前端还提示"填入已有 ID 可复用其历史偏好", 把越权入口写成了功能。
    3. 现在: 只有 `team_<32位hex>` 形态被复用 —— 那正是本服务自己生成的
       不透明随机 id(不可枚举, 也不是人取的名字), 其余(用户手填的)一律忽略
       并重新生成。猜中一个词的代价变成猜中 32 位随机。
       代价: 不再支持"手填一个团队名跨设备共享" —— 那本来就不该靠明文 id 实现,
       真要做需要签名 token(待办), 不能拿隔离当鉴权。
    """
    cleaned = (raw or "").strip()
    if _TEAM_ID_RE.match(cleaned):
        return cleaned
    return f"team_{uuid.uuid4().hex}"


async def get_event_graph():
    """获取编译后的 LangGraph 实例(带 AsyncSqliteSaver checkpointer 与 AsyncSqliteStore, 首次懒加载)"""
    global _event_graph, _saver, _store
    if _event_graph is None:
        conn = await aiosqlite.connect(str(_DB_PATH))
        _saver = AsyncSqliteSaver(conn)
        # isolation_level=None(autocommit): AsyncSqliteStore 内部自管事务(显式 BEGIN),
        # 默认隔离级会让 setup 的 INSERT 留下未提交事务, 导致后续 BEGIN 报
        # "cannot start a transaction within a transaction"
        store_conn = await aiosqlite.connect(str(_STORE_PATH), isolation_level=None)
        _store = AsyncSqliteStore(store_conn)
        await _store.setup()   # 建表(幂等)
        _event_graph = build_event_graph(checkpointer=_saver, store=_store)
        # 启动时清一次僵尸 thread(用户提交后再也没回来审批的),
        # 否则 checkpoints.db 只增不减, 长期跑必然吃满磁盘
        try:
            swept = await _sweep_stale_threads(_CHECKPOINT_TTL_DAYS)
            if swept:
                print(f"🧹 启动清理: 删除 {swept} 个超过 {_CHECKPOINT_TTL_DAYS} 天的僵尸 thread")
        except Exception as e:
            print(f"⚠️  启动 checkpoint 清理失败(不阻塞服务): {str(e)[:120]}")
    return _event_graph


def _db_writable(path: Path) -> bool:
    """探活: 该 sqlite 库当前是否可写(不做任何数据改动)

    为什么需要: checkpointer 写失败时整条 /plan 会抛 bare 500, 而 /health 仍报
    healthy —— 运维/前端只能靠翻日志才能定位。这里提前把状态暴露出来。
    """
    import sqlite3

    try:
        conn = sqlite3.connect(str(path), timeout=3.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return True
        finally:
            conn.close()
    except Exception:
        return False


def _checkpoint_stats() -> dict:
    """checkpoint 库体积统计(挂 /health): 判断清理机制是否真的在起作用

    checkpoints 表的 metadata **没有时间戳字段**(实测 keys 只有
    parents/source/step), 所以无法直接按时间过滤; 好在 checkpoint_id 是
    uuid6(时间有序), 可解析出写入时刻做 TTL 判定, 见 _sweep_stale_threads。
    """
    import sqlite3

    stats = {"threads": 0, "checkpoints": 0, "writes": 0, "size_mb": 0.0}
    try:
        stats["size_mb"] = round(_DB_PATH.stat().st_size / 1024 / 1024, 2) \
            if _DB_PATH.exists() else 0.0
        conn = sqlite3.connect(str(_DB_PATH), timeout=3.0)
        try:
            stats["threads"] = conn.execute(
                "SELECT COUNT(DISTINCT thread_id) FROM checkpoints").fetchone()[0]
            stats["checkpoints"] = conn.execute(
                "SELECT COUNT(*) FROM checkpoints").fetchone()[0]
            # writes 是 LangGraph 本版本的实际表名(不是 checkpoint_writes)
            stats["writes"] = conn.execute(
                "SELECT COUNT(*) FROM writes").fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        stats["error"] = str(e)[:120]
    return stats


async def _drop_thread(thread_id: str, reason: str) -> None:
    """删除一个 thread 的全部 checkpoint(用完即删, 防止库无限膨胀)

    何时调用(按生命周期分层, 都是"这个 thread 不会再被 resume"之后):
    - /parse /preferences: 一次性计算端点, 返回后立即删
    - /resume 且流程已 completed: 响应组装完再删(此时 result 已在内存里)
    失败只记日志不抛: 清理是运维性动作, 不该让业务请求 500。
    """
    if _saver is None:
        return
    try:
        await _saver.adelete_thread(thread_id)
        print(f"🧹 checkpoint 已清理({reason}): {thread_id}")
    except Exception as e:
        print(f"⚠️  checkpoint 清理失败({reason}): {thread_id} -> {str(e)[:120]}")


def _find_stale_threads(max_age_days: int = 7) -> List[str]:
    """找出超过 max_age_days 天未活动的 thread_id 列表(同步, 只读 sqlite)

    覆盖前两层清理不到的"僵尸 thread": 用户提交需求后再也没回来审批。
    时间基准取每个 thread **最新的 checkpoint_id**(uuid6 高 60 位是 100ns
    精度的格里高利历时间戳, 已实测可解析出正确日期)。metadata 里没有时间戳
    字段(keys 只有 parents/source/step), 所以只能靠 uuid6。
    """
    import sqlite3
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone

    if not _DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT thread_id, MAX(checkpoint_id) FROM checkpoints GROUP BY thread_id"
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        print(f"⚠️  checkpoint TTL 扫描失败(读): {str(e)[:120]}")
        return []

    gregorian_offset_ms = 12219292800000
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    total = len(rows)
    stale: List[str] = []
    for thread_id, latest_id in rows:
        try:
            unix_ms = (_uuid.UUID(latest_id).int >> 68) / 1e4 - gregorian_offset_ms
            last_at = datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc)
        except Exception:
            continue    # id 格式异常就不删, 保守处理
        if last_at < cutoff:
            stale.append(thread_id)

    if stale:
        print(f"🧹 checkpoint TTL 扫描: {len(stale)}/{total} 个 thread 超过 {max_age_days} 天未活动")
    return stale


async def _sweep_stale_threads(max_age_days: int = 7) -> int:
    """TTL 扫描并删除僵尸 thread, 返回删除条数(启动时调用一次)"""
    if _saver is None:
        return 0
    stale = await asyncio.to_thread(_find_stale_threads, max_age_days)
    for tid in stale:
        await _drop_thread(tid, "TTL过期")
    return len(stale)


def _graph_failure(exc: Exception, action: str) -> HTTPException:
    """把图执行异常转成带可读明细的 500(而不是裸 Internal Server Error)"""
    msg = str(exc)
    hint = ""
    if "readonly database" in msg or "unable to open database" in msg:
        hint = (f" (checkpoint 库不可写: 请检查 {_DB_PATH.parent} 的目录/文件写权限, "
                f"或是否有其它进程占用 checkpoints.db)")
    elif "recursion_limit" in msg:
        hint = " (流程回环超过递归上限, 请检查 reconcile 回炉次数)"
    return HTTPException(
        status_code=500,
        detail=f"{action}失败: {type(exc).__name__}: {msg}{hint}",
    )


async def _invoke_graph(graph, payload, cfg: dict, action: str) -> dict:
    """统一的图执行入口: 分类过的失败按类型给状态码, 其余转成带明细的 500

    分两层:
      - `AppError`(上游抖动/能力未开/配置错/坏响应)  -> 503/501/500/502 + 面向
        用户的文案, 不往外吐上游细节;
      - 其余异常(程序缺陷) -> 仍是 500, 但**先把完整栈打进日志**再去转译。
        旧实现只 print 一行消息就转译, 等于 bug 在日志里也只留一行字, 没有栈。
    """
    try:
        return await graph.ainvoke(payload, config=cfg)
    except HTTPException:
        raise
    except AppError as exc:
        print(f"[graph] {action}失败({exc.code}): {str(exc)[:200]}")
        raise to_http_error(exc, action)
    except Exception as exc:   # noqa: BLE001  程序缺陷: 打全栈后再转成可读错误
        import traceback
        # 纯 ASCII 前缀: 本机控制台是 GBK, 带 emoji 的 print 会抛 UnicodeEncodeError
        # —— 那会在 except 块里再抛一个异常, 把真正的失败原因顶掉
        print(f"[graph] {action}异常: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        raise _graph_failure(exc, action)


def _thread_config(thread_id: str) -> dict:
    """构造带 thread_id 的运行配置(checkpointer 必需)"""
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": _RECURSION_LIMIT,
    }


def _extract_interrupt(result: dict, graph, cfg: dict) -> Optional[dict]:
    """从 ainvoke 结果/状态快照中提取 interrupt payload(兼容两种返回形态)"""
    intr = result.get("__interrupt__") if isinstance(result, dict) else None
    if intr:
        first = intr[0]
        return first.value if hasattr(first, "value") else first
    # 兜底: 从状态快照的 pending task 里取
    try:
        snap = graph.get_state(cfg)  # sync 版本; async 图下仅作兜底
        for t in snap.tasks or []:
            for i in getattr(t, "interrupts", None) or []:
                return i.value
    except Exception:
        pass
    return None


class EventParseBody(BaseModel):
    """需求解析请求体(自由文本入口)"""
    raw_text: str = Field(..., min_length=1, max_length=500, description="用户原始输入(自由文本)", example="10月底组织30人团建，预算3万，上海周边一天")
    team_id: Optional[str] = Field(default=None, description="团队ID(可选; 有历史活动记录时解析 prompt 会注入'上次你们……'上下文)")


class PlanStartBody(BaseModel):
    """发起策划流程请求体(**结构化**, 非自由文本)

    为什么直接收结构化字段而不收句子: 前端表单本来就握着精确值
    (人数/日期区间/预算/出发地), 把它们拼成一句话再让 LLM 抽回来是纯亏 ——
    既多花一次 LLM, 又会抽错(如"人均预算1000元(总30000元)"到底算哪个)。
    这里把口径钉死在入口: 人数/开始日期/出发地为必填, 预算一律为**总预算**,
    日期一律为 **YYYY-MM-DD**。搭配 parse_request 的结构化分支, 这条主路径
    零 LLM 调用。

    自由文本入口保留在 /parse(那里缺字段是正常结果, 才需要 missing_fields 追问);
    raw_text 在本模型里只是可选的"原话", 供日志/复盘留痕, 不参与抽取。
    """
    people_count: int = Field(..., ge=1, le=500, description="参与人数")
    date: _date = Field(..., description="活动开始日期 YYYY-MM-DD")
    date_end: Optional[_date] = Field(default=None, description="活动结束日期(多日活动; 单日留空)")
    budget: Optional[float] = Field(default=None, ge=0, description="总预算(元; 非人均)")
    origin: str = Field(..., min_length=1, max_length=50, description="出发地/集合地")
    destination: Optional[str] = Field(default=None, max_length=50, description="目的地(同城可留空)")
    origin_place: Optional[PlaceInput] = Field(
        default=None,
        description="出发地的省市县选择 + 更具体地点(可选; 给了就不再猜归属)")
    destination_place: Optional[PlaceInput] = Field(
        default=None,
        description="目的地的省市县选择 + 更具体地点(可选; 给了就不再猜归属)")
    transport_pref: Optional[str] = Field(default=None, max_length=20, description="优先通行方式")
    activity_type: Optional[str] = Field(default=None, max_length=30, description="活动类型,如 团建/生日会")
    raw_text: Optional[str] = Field(
        default=None, max_length=500,
        description="用户原话(可选; 仅留痕展示, 不参与结构化抽取)")
    team_id: Optional[str] = Field(default=None, description="团队ID(可选; 服务端签发的不透明 id 才会被复用, 手填的会被忽略并重新生成)")
    preferences: Optional[List[PersonPref]] = Field(
        default=None, max_length=200,
        description="成员偏好(可选; 随 /plan 一次提交, 无需再调 /preferences)。"
                    "每条 PersonPref.headcount 声明该行代表几个人: 逐人填写=1, "
                    "一条'全员汇总'=总人数")

    @field_validator("date", "date_end")
    @classmethod
    def _not_in_past(cls, v: Optional[_date]) -> Optional[_date]:
        """活动日期不得早于今天: 放行会让天气/火车票去查过去的日期"""
        if v is not None and v < _date.today():
            raise ValueError(f"活动日期不能早于今天({_date.today().isoformat()})")
        return v

    @model_validator(mode="after")
    def _check_range(self) -> "PlanStartBody":
        """结束日期不得早于开始日期"""
        if self.date_end and self.date_end < self.date:
            raise ValueError("date_end 不能早于 date")
        return self

    @model_validator(mode="after")
    def _check_headcount(self) -> "PlanStartBody":
        """声明人数之和不得超过总人数

        headcount 超过 people_count 会让票数凭空超过总人数(审批页写出"35人想爬山"
        而团队只有 30 人)。**不要求相等**: 差额是"未表态", 是正常情况(只收集了
        部分成员偏好); 但超过总人数一定是调用方算错了。
        这一层只覆盖 HTTP 路径 —— evals/直连图绕过本模型, 那边由 aggregate_prefs
        的日志兜底(未表态人数会打出来)。
        """
        if self.preferences:
            declared = sum(p.headcount for p in self.preferences)
            if declared > self.people_count:
                raise ValueError(
                    f"preferences 声明人数之和({declared})超过总人数({self.people_count})"
                )
        return self


class PlanStartResponse(BaseModel):
    """发起/恢复策划流程响应(HITL)"""
    success: bool = Field(..., description="是否成功")
    thread_id: str = Field(..., description="流程线程ID, resume 时回传")
    team_id: Optional[str] = Field(
        default=None,
        description="本次使用的团队ID(服务端签发的不透明 id; 前端应持久化后续复用。"
                    "请求里手填的 id 会被忽略 —— 明文可猜的 id 不能当访问凭证)",
    )
    status: str = Field(..., description="pending_approval(待审批) / completed(已完成)")
    message: str = Field(default="", description="消息")
    approval_payload: Optional[dict] = Field(default=None, description="interrupt payload: 最终方案/预算明细/分歧点/风险备选")
    data: Optional[EventPlan] = Field(default=None, description="status=completed 时的最终方案")


class ResumeBody(BaseModel):
    """审批恢复请求体"""
    thread_id: str = Field(..., min_length=1, description="发起流程时返回的线程ID")
    approved: bool = Field(..., description="是否通过审批")
    feedback: Optional[str] = Field(default=None, description="否决时的修改意见(approved=False 时生效, 注入重生成 prompt)")
    selections: Optional[dict] = Field(
        default=None,
        description="多候选挑选结果(可选): {\"food\": \"候选餐厅名\", \"transport\": \"高铁/火车\"}; "
                    "key=提案类别, value=审批页从 Proposal.options 中勾选的候选名")


@router.post(
    "/parse",
    response_model=EventRequest,
    summary="需求解析: 原始文本 -> 结构化 EventRequest",
    description="自由文本入口(唯一会调 LLM 做抽取的端点): 抽取 人数/日期/预算/出发地/活动类型; "
                "缺关键字段(人数/日期/出发地)或日期早于今天返回 400 + missing_fields, 不进入后续编排。"
                "表单类前端请改走 /plan 的结构化入参, 少一次 LLM 且不会抽错。"
)
async def parse_event(body: EventParseBody):
    """需求解析(自由文本)

    Args:
        body: 含 raw_text 的请求体

    Returns:
        结构化 EventRequest; 缺关键字段时 HTTP 400 + missing_fields
    """
    raw_text = body.raw_text.strip()
    if not raw_text:
        # 程序化前置校验: 空文本直接 400, 不进图不耗 LLM
        raise HTTPException(
            status_code=400,
            detail={"message": "raw_text 不能为空", "missing_fields": ["raw_text"]},
        )

    print(f"\n{'='*60}")
    print(f"📥 收到需求解析请求:")
    print(f"   原始输入: {raw_text}")
    print(f"{'='*60}\n")

    graph = await get_event_graph()
    thread_id = str(uuid.uuid4())   # 轻量流程一次性线程
    final_state = await _invoke_graph(
        graph,
        {"raw_request": {"raw_text": raw_text, "team_id": body.team_id}},
        _thread_config(thread_id),
        "需求解析",
    )
    # 一次性线程: 解析结果已拿到内存, checkpoint 不会再被 resume 读 -> 立即清理
    await _drop_thread(thread_id, "一次性/parse")

    steps = final_state.get("steps_log") or []
    print(f"✅ 图执行完成, 步骤: {' -> '.join(steps)}\n")

    parsed = final_state.get("parsed_request") or {}
    missing = final_state.get("missing_fields") or []

    # 缺关键字段 -> 400 + missing_fields 表单提示, 不进后续编排
    # (parse_request 把"日期早于今天"也计入 missing_fields, 所以这里一并挡下)
    key_missing = [f for f in KEY_FIELDS if f in missing]
    if key_missing:
        print(f"⚠️ 缺关键字段: {key_missing}, 返回 400\n")
        raise HTTPException(
            status_code=400,
            detail={
                "message": "缺少关键信息(或日期已过期), 请补充后再试",
                "missing_fields": key_missing,
                "missing_all": missing,
                "parsed": parsed,   # 已解析出的部分, 供前端表单预填
            },
        )

    return EventRequest(**parsed)


@router.post(
    "/plan",
    response_model=PlanStartResponse,
    summary="发起团建活动策划流程(HITL: 生成后暂停等待人工审批)",
    description="**结构化入参**(人数/日期/出发地必填; 预算=总预算; 日期=YYYY-MM-DD)。"
                "编排: 需求组装(零 LLM) -> 偏好聚合 -> 基础信息采集 -> 四路并行提案 -> reconcile 约束消解"
                "(回环) -> risk 风险评估 -> approval 人工审批(interrupt 暂停)。"
                "返回 thread_id + approval_payload; 用 POST /resume 提交审批结果继续流程。"
                "审批通过后走 checklist 任务清单 + memory 团队记忆写入。"
                "状态经 AsyncSqliteSaver 持久化到 backend/data/checkpoints.db, 跨进程可恢复。"
)
async def plan_event(request: PlanStartBody):
    """发起团建活动策划: 跑到审批闸门并挂起

    Args:
        request: 结构化需求(人数/日期/出发地必填) + 可选偏好
                 schema 不合法(缺必填/日期在过去/超长)由 FastAPI 直接 422,
                 根本进不到图里 —— 旧版这里不校验, 空需求也会跑完整流程。

    Returns:
        PlanStartResponse: thread_id + status=pending_approval + approval_payload
    """
    print(f"\n{'='*60}")
    print(f"📥 收到团建活动策划请求(全流程, 结构化):")
    print(f"   人数={request.people_count}, 日期={request.date}"
          f"{'~' + request.date_end.isoformat() if request.date_end else ''}, "
          f"总预算={request.budget}, 出发地={request.origin}"
          f"{' -> ' + request.destination if request.destination else ''}")
    print(f"   原话: {request.raw_text or '(无)'}")
    print(f"{'='*60}\n")

    graph = await get_event_graph()
    thread_id = str(uuid.uuid4())
    cfg = _thread_config(thread_id)
    # team_id 归一: 未传/手填的一律生成新的不透明 id 并回传(前端持久化)
    team_id = _resolve_team_id(request.team_id)
    # 结构化字段进 raw_request["structured"], parse_request 直接组装(零 LLM);
    # 日期转 ISO 字符串(状态要可序列化, 且下游全按字符串比较)
    structured = {
        "people_count": request.people_count,
        "date": request.date.isoformat(),
        "date_end": request.date_end.isoformat() if request.date_end else None,
        "budget": request.budget,
        "origin": request.origin.strip(),
        "destination": (request.destination or "").strip() or None,
        "transport_pref": request.transport_pref or None,
        "activity_type": request.activity_type or None,
        # 地点入参只进 structured, **不进 EventRequest**: EventRequest 同时是自由
        # 文本路径的 LLM 工具 schema(nodes.py:244), 挂上去会让 LLM 幻觉出"权威
        # 归属", 而 hint 会跳过 _name_related 等闸门 —— 假的权威比没有更糟。
        # 路径 A 的 allowed 过滤会忽略这两个键, gather_info 直接读 structured。
        "origin_place": request.origin_place.model_dump() if request.origin_place else None,
        "destination_place": request.destination_place.model_dump() if request.destination_place else None,
    }
    init_state: dict = {
        "raw_request": {"raw_text": (request.raw_text or "").strip(),
                        "team_id": team_id,
                        "structured": structured},
        "generate_plan": True,   # 门控: 允许进入四路提案
    }
    if request.preferences:
        # 偏好随 /plan 直接进 state, aggregate_prefs 同轮消费; 也是唯一入口
        # (原 /preferences 分批累加已下线: 那份 _pref_store 是进程内字典,
        #  重启即丢, 而 team_id 是持久的 -> 生命周期不一致必然对不上号)
        init_state["preferences"] = [p.model_dump() for p in request.preferences]
    result = await _invoke_graph(graph, init_state, cfg, "策划流程")

    payload = _extract_interrupt(result, graph, cfg)
    if payload is not None:
        print(f"⏸️  流程暂停于审批闸门, thread_id={thread_id}, team_id={team_id}\n")
        return PlanStartResponse(
            success=True,
            thread_id=thread_id,
            team_id=team_id,
            status="pending_approval",
            approval_payload=payload,
            message="方案已生成, 等待人工审批(POST /event/resume 提交结果)",
        )

    # 兜底: approval 正常必 interrupt; 走到这里说明图异常直通 END
    steps = " -> ".join(result.get("steps_log") or [])
    print(f"✅ 图执行完成(未暂停审批?): {steps}\n")
    return PlanStartResponse(
        success=True,
        thread_id=thread_id,
        team_id=team_id,
        status="completed",
        message="流程已完成(未经审批, 异常路径)",
        data=_event_plan_from_state(result),
    )


def _event_plan_from_state(final_state: dict) -> EventPlan:
    """从图终态组装 EventPlan(四路提案 + 冲突点 + 风险文本)"""
    proposals_dict = final_state.get("proposals") or {}
    proposals = [
        Proposal(**proposals_dict[key]) for key in ("transport", "venue", "flow", "food")
        if key in proposals_dict
    ]
    if not proposals:
        raise HTTPException(status_code=500, detail="四路提案均未产出, 请检查采集数据")

    pref = final_state.get("pref_summary")
    conflict_points = pref.conflict_points if pref is not None and not isinstance(pref, dict) \
        else (pref or {}).get("conflict_points", [])

    # 约束消解结论 + 风险评估结论 + 审批结论 -> risk_report 文本
    report = final_state.get("conflict_report") or {}
    replan_count = final_state.get("replan_count") or 0
    rc_conflicts = report.get("conflicts") or []
    if rc_conflicts:
        parts = [f"约束消解未完全通过(回炉{replan_count}次), 需人工裁决: " + "; ".join(rc_conflicts)]
    else:
        parts = [f"约束消解通过(共校验{replan_count + 1}次): 预算/时间线/容量均满足"]

    # 风险结论**按最终方案现算**(纯函数零成本), 而不是引用暂停那一刻的快照:
    # 审批时换了候选/否决重走之后, 旧快照描述的是另一份方案 —— 交付物里说
    # "低风险"、而用户挑的是室外场地, 就是这么来的。
    pref_summary = final_state.get("pref_summary")
    if pref_summary is not None and not isinstance(pref_summary, dict):
        pref_summary = pref_summary.model_dump()
    risk = reassess_risk(
        final_state.get("risk_report") or {},
        final_state.get("basic_info") or {},
        final_state.get("parsed_request") or {},
        final_state.get("proposals") or {},
        pref_summary,
    )
    risk_flags = risk.get("flags") or []
    # unknown 必须原样写进交付物。写成"低风险"就等于把"没查"当成了"查了没事" ——
    # 这条文案正是用户唯一会读到的风险结论
    level_text = {"high": "高风险", "medium": "中风险", "low": "低风险",
                  "unknown": "未评估"}.get(risk.get("level"), "未评估")
    if risk_flags:
        parts.append(f"风险评估({level_text}): "
                     + "; ".join(f.get("detail", "") for f in risk_flags))
        for alt in risk.get("alternatives") or []:
            parts.append(f"备选: {alt.get('plan')}(约{alt.get('est_cost', 0):.0f}元, "
                         f"模型估算, 未计入合计)")
        if risk.get("uncovered_flags"):
            parts.append(f"以下风险暂无备选: {'; '.join(risk['uncovered_flags'])}")
    elif risk.get("level") == "unknown":
        gap = "; ".join(f"{r.get('name') or r.get('rule')}: {r.get('reason')}"
                        for r in (risk.get("rules") or [])
                        if r.get("status") == "not_evaluated" and r.get("reason"))
        parts.append(f"风险评估({level_text}): 未能判定, 信息不足({gap or '缺天气/体能等输入'})")
    else:
        parts.append(f"风险评估({level_text}): {risk.get('summary') or '无需备选'}")
    if risk.get("unevaluated_inputs"):
        parts.append(f"未纳入判定的项目(强度/室内外无法识别): "
                     + "、".join(risk["unevaluated_inputs"]))

    decision = final_state.get("approval_decision") or {}
    if decision:
        parts.append(f"人工审批: {'已通过' if decision.get('approved') else '曾否决'}"
                     + (f", 意见: {decision.get('feedback')}" if decision.get("feedback") else ""))

    return EventPlan(
        proposals=proposals,
        conflict_points=conflict_points,
        risk_report=" | ".join(parts),
        pref_coverage={},     # 后续 Phase 产出
        checklist=final_state.get("checklist"),
        # 记忆写没写成由写的那一步给出结论(它可能降级放行), 交付页照着说
        memory_status=final_state.get("memory_status"),
    )


@router.post(
    "/resume",
    response_model=PlanStartResponse,
    summary="提交审批结果并恢复流程(HITL)",
    description="approved=True: Command(resume) 放行 -> 流程结束返回最终方案; "
                "approved=False+feedback: 意见写入 user_feedback -> 路由回 aggregate_prefs 重走, "
                "意见注入四路提案 prompt, 生成后再次暂停等待审批。"
                "状态从 checkpoint 恢复, 支持进程重启后继续(thread_id 不变)。"
)
async def resume_event(body: ResumeBody):
    """恢复被审批闸门暂停的流程

    Args:
        body: thread_id + approved + feedback(否决时)

    Returns:
        PlanStartResponse: approved=True -> status=completed + data(最终方案);
                          approved=False -> status=pending_approval + 新的 approval_payload
    """
    print(f"\n{'='*60}")
    print(f"📥 收到审批结果: thread_id={body.thread_id}, approved={body.approved}, "
          f"feedback={body.feedback or '无'}")
    print(f"{'='*60}\n")

    graph = await get_event_graph()
    cfg = _thread_config(body.thread_id)

    # 前置检查: 线程必须存在且有暂停中的审批
    snap = await graph.aget_state(cfg)
    if not snap.values:
        raise HTTPException(status_code=404, detail="thread 不存在或已过期")
    if not snap.next:
        raise HTTPException(status_code=400, detail="该线程流程已结束, 无待审批事项")

    # team_id 从 checkpoint 状态回读(审批否决重走时 /plan 早已归一过一次)
    team_id = (snap.values or {}).get("team_id")

    result = await _invoke_graph(
        graph,
        Command(resume={
            "approved": body.approved,
            "feedback": body.feedback or "",
            "selections": body.selections or {},
        }),
        cfg,
        "审批恢复",
    )

    payload = _extract_interrupt(result, graph, cfg)
    if payload is not None:
        # approved=False: 重走一轮后又到审批闸门, 返回新 payload
        print(f"⏸️  按意见重新生成后再次暂停审批, thread_id={body.thread_id}\n")
        return PlanStartResponse(
            success=True,
            thread_id=body.thread_id,
            team_id=team_id,
            status="pending_approval",
            approval_payload=payload,
            message="已按修改意见重新生成方案, 等待再次审批",
        )

    steps = " -> ".join(result.get("steps_log") or [])
    print(f"✅ 流程恢复并完成: {steps}\n")
    # 流程已完成 -> 组装完响应即可清理 checkpoint(此时 result 已在内存里,
    # 之后不会再 resume 这个 thread); 否决重走分支(payload 非空)必须保留
    final_plan = _event_plan_from_state(result)
    await _drop_thread(body.thread_id, "流程完成")
    return PlanStartResponse(
        success=True,
        thread_id=body.thread_id,
        team_id=team_id,
        status="completed",
        message="审批通过, 流程完成",
        data=final_plan,
    )


@router.get(
    "/health",
    summary="健康检查",
    description="检查团建活动策划图是否正常(含 checkpoint/store 可写性探活)"
)
async def health_check():
    """健康检查

    注意: 图能编译不等于能跑。/plan 需要往 checkpoints.db 写状态, 该库不可写时
    整条流程会 500, 所以这里额外做一次可写性探活并如实回报 degraded。
    """
    try:
        graph = await get_event_graph()
        checkpoint_ok = _db_writable(_DB_PATH)
        store_ok = _db_writable(_STORE_PATH)
        healthy = checkpoint_ok and store_ok
        ckpt_stats = _checkpoint_stats()
        return {
            "status": "healthy" if healthy else "degraded",
            "service": "event-planner",
            "engine": "langgraph",
            "checkpointer": f"AsyncSqliteSaver({_DB_PATH})",
            "store": f"AsyncSqliteStore({_STORE_PATH})",
            "checkpoint_writable": checkpoint_ok,
            "store_writable": store_ok,
            "detail": ("" if healthy else
                       f"checkpoint/store 库不可写 -> /plan 与 /resume 会失败; "
                       f"请检查 {_DB_PATH.parent} 的目录与文件写权限"),
            "checkpoints": ckpt_stats,
            # 外网调用缓存: TTL 分级 + 负结果不写长缓存(见 graph/ttl_cache.py)
            "caches": cache_stats(),
            "nodes": [
                "parse_request", "aggregate_prefs", "gather_info",
                "propose_transport", "propose_venue", "propose_flow", "propose_food",
                "reconcile", "risk", "approval", "checklist", "memory",
            ],
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"服务不可用: {str(e)}"
        )
