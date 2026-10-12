"""外部调用失败的分类与处置(全后端统一)

旧实现把所有失败压成两种表达——空列表 or 500, 于是:

1. **参数错和网络抖动被当成同一类**: `invoke_structured` 里 pydantic 校验失败
   和 httpx 超时共用一个计数器裸重试, 还不告诉模型错在哪。前者该让模型改参数,
   后者该退避重试, 处置完全不同。
2. **"没有数据"和"调用失败"分不出来**: 三个客户端失败时都 `return []`, 调用方
   无法区分"这个城市真没火车"和"聚合数据挂了"。
3. **`except Exception` 把程序缺陷抹平成空值**: 字段名写错、schema 对不上这类
   错误不会报错, 而是静默产出一个"看起来正常、其实少数据"的方案。

本模块给失败建立类型。**四类失败的下一步完全不同**:

    ┌ 参数格式错 / 缺必填字段      -> Schema 拦截; 带修正指令回给模型重填
    ├ 正常业务结果(无结果/无权限)  -> 不是错误; 讲清原因, 交给 Agent 决策
    ├ 临时故障(超时/限流/暂不可用) -> 有上限重试(退避 + 次数 + 总时长预算)
    └ 程序缺陷 / 数据损坏 / 配置错  -> 不吞, 冒出来让人看到

**核心规则(贯穿全后端)**: 边界上只捕 `AppError`, 其余异常一律让它冒出来。

    try:
        ...
    except AppError as e:      # 我知道这是什么、也知道怎么办
        ...
    # 其余异常不捕 -> 冒到 /health / _graph_failure -> 被人看到

这条规则让"程序缺陷不吞"变成机械可执行的, 不需要逐处判断。
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import httpx

try:  # LLM 走 openai SDK: 它的异常**不继承 httpx**(只把 httpx 异常挂在 __cause__),
    # 不认它的话 "LLM 超时" 会被 classify 判成"认不出来"而直接穿出重试层
    import openai
except ImportError:  # pragma: no cover - 依赖可选, 缺了只是少认一类异常
    openai = None  # type: ignore[assignment]

# 三个外部客户端共用的单次请求超时(此前高德 10s / 口碑与火车 15s, 不统一)。
# 这些调用处在用户等待的策划流程里、且是并行发起, 总体延迟由最慢的一路决定,
# 所以超时值要统一且偏紧; 单次超时之外的"重试到什么时候放弃"由 retry_budget 管。
DEFAULT_HTTP_TIMEOUT = 10.0

# ============================ 异常分类 ============================


class AppError(Exception):
    """所有"我知道怎么处理"的失败的基类

    子类通过三个类属性回答"该怎么办", 调用方据此决策而不是靠 isinstance 猜:
        retryable          是否可以重试(必须有退避+次数+总时长上限)
        feed_back_to_model 是否该把错误回给模型让它改(而不是原样重发)
        user_message       给用户看的文案
    """

    code: str = "app_error"
    retryable: bool = False
    feed_back_to_model: bool = False
    user_message: str = "服务暂时不可用, 请稍后重试"

    def __init__(self, message: str, *, code: Optional[str] = None,
                 user_message: Optional[str] = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if user_message is not None:
            self.user_message = user_message


class TransientError(AppError):
    """临时故障: 超时 / 限流 / 5xx / 连接失败

    可以尝试恢复, 但**必须有上限**: 退避 + 次数 + 总时长预算。
    少了任何一样, Agent 就会在一个坏掉的服务前反复等待。
    """

    code = "transient"
    retryable = True
    user_message = "上游服务暂时不可用, 已尝试重试"


class ModelOutputError(AppError):
    """模型输出不符合 schema: 缺 tool_calls, 或参数过不了 pydantic 校验

    这类错误**不该裸重试原 prompt** —— 错在模型给的参数, 正确的处置是把
    "哪个字段、错在哪"拼成修正指令回给模型重填一次。所以它和 TransientError
    必须分开计数、分开记日志, 否则"模型反复给错参数"和"服务偶发抖动"会混在一起。
    """

    code = "model_output"
    feed_back_to_model = True
    user_message = "模型输出格式异常"


class PermanentError(AppError):
    """永久错误: 配置错 / 权限错 / 数据损坏

    不重试、不吞。必须冒出来被人看到, 否则系统会带着坏配置一直跑下去。
    """

    code = "permanent"
    user_message = "服务配置或数据异常, 请联系管理员"


class ConfigError(PermanentError):
    """**必需**的配置缺失(如 AMAP_API_KEY 未设置)

    启动期或首次调用时就该暴露, 而不是降级成空结果继续跑。
    """

    code = "config"
    user_message = "服务缺少必要配置, 请联系管理员"


class CapabilityDisabled(AppError):
    """**可选**数据源未接入(如没配 BOCHA_API_KEY / JUHE_TRAIN_KEY) —— 不是故障

    与 ConfigError 的分界:
      - ConfigError         必需配置缺失 -> 系统不该继续跑
      - CapabilityDisabled  可选能力未开 -> 流程照跑, 只是少一维数据

    旧代码里"没配 key"和"接口挂了"都是 `[]`, 调用方分不出来。现在由
    `call_external` 映射成 `CallStatus.DISABLED`: 不重试、不算错误、不报降级,
    但对外仍是"这一维没有数据"的显式说明。
    """

    code = "capability_disabled"
    retryable = False
    user_message = "该数据源未启用"


def classify(exc: BaseException, *, kind: str = "") -> Optional[AppError]:
    """把任意异常分成 AppError; **认不出来返回 None 表示"不该捕"**

    这是上面那条核心规则的实现点:
      - 返回 AppError -> 调用方知道该怎么办(重试 / 回给模型 / 报错)
      - 返回 None     -> 这是程序缺陷或未知错误, 调用方必须让它冒出来

    `kind` 只用于日志前缀(如 "amap" / "bocha"), 不进类型判断。
    """
    if isinstance(exc, AppError):
        return exc

    label = f"[{kind}] " if kind else ""

    # 超时: 要在 TransportError 之前判, 否则被笼统归成"传输失败", 日志看不出是超时
    if isinstance(exc, httpx.TimeoutException):
        return TransientError(f"{label}请求超时: {exc}", code="timeout")
    # 连接被拒 / DNS 失败 / 读到一半断流 / 协议错
    if isinstance(exc, httpx.TransportError):
        return TransientError(f"{label}连接或传输失败: {exc}", code="transport")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else 0
        # 5xx 与 429 是可恢复的; 4xx 其它多为调用方问题(参数/权限), 重试无意义
        if status >= 500 or status == 429:
            return TransientError(f"{label}上游返回 {status}: {exc}", code="upstream_5xx")
        return PermanentError(f"{label}上游返回 {status}: {exc}", code="upstream_4xx")
    if isinstance(exc, httpx.HTTPError):
        return TransientError(f"{label}HTTP 异常: {exc}", code="http")
    # 上游返回的不是 JSON(常见于网关错误页): 视为临时故障
    if isinstance(exc, json.JSONDecodeError):
        return TransientError(f"{label}上游返回非 JSON: {exc}", code="bad_payload")

    # LLM(openai SDK)那一套并行的异常族
    llm_err = _classify_openai(exc, label)
    if llm_err is not None:
        return llm_err

    # 认不出来 —— 程序缺陷? 未知异常? 一律不捕, 让它冒出来。
    # 注意: 这里**故意不匹配 bare ValueError / TypeError**, 因为字段名写错、
    # 解包数量不对这类程序 bug 也是 ValueError, 吞掉它们正是本次要修的病。
    return None


def _classify_openai(exc: BaseException, label: str) -> Optional[AppError]:
    """LLM(openai SDK)自己的异常族 —— 与 httpx 完全平行的一套类型

    映射口径与上面 httpx 的一致: 值得再试一次的(超时/连接/限流/5xx)进
    TransientError, 再试也没意义的(鉴权/参数/权限/模型不存在)进 PermanentError。
    """
    if openai is None or not isinstance(exc, openai.OpenAIError):
        return None

    # APITimeoutError 继承自 APIConnectionError, 必须先判超时, 否则日志里
    # 看不出到底是"等超时了"还是"连不上"
    if isinstance(exc, openai.APITimeoutError):
        return TransientError(f"{label}LLM 请求超时: {exc}", code="llm_timeout")
    if isinstance(exc, openai.APIConnectionError):
        return TransientError(f"{label}LLM 连接失败: {exc}", code="llm_transport")
    if isinstance(exc, openai.RateLimitError):
        return TransientError(f"{label}LLM 限流: {exc}", code="llm_rate_limit")
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status >= 500:
            return TransientError(f"{label}LLM 上游返回 {status}: {exc}",
                                  code="llm_5xx")
        if status == 401:
            # key 错了/过期 —— 属于必需配置问题, 重试一万次也是 401
            return ConfigError(f"{label}LLM 鉴权失败({status}), 请检查 LLM_API_KEY: {exc}",
                               user_message="模型服务鉴权失败, 请联系管理员检查配置")
        return PermanentError(f"{label}LLM 上游返回 {status}: {exc}", code="llm_4xx")
    if isinstance(exc, openai.APIResponseValidationError):
        # 返回体不是它自己声明的结构(网关错误页等): 与"上游返回非 JSON"同类
        return TransientError(f"{label}LLM 返回体结构异常: {exc}", code="llm_bad_payload")
    return PermanentError(f"{label}LLM 调用失败: {exc}", code="llm_error")


# ============================ 调用结果 ============================


class CallStatus(str, Enum):
    """一次外部调用的结局(比"空列表"多出四种语义)"""

    OK = "ok"              # 拿到数据
    EMPTY = "empty"        # 正常业务结果: 确实没有数据, **不该重试**
    DISABLED = "disabled"  # 该能力未配置(如没配博查 key), 刻意降级, **不是故障**
    DEGRADED = "degraded"  # 临时故障且重试预算已耗尽
    FAILED = "failed"      # 永久错误, 重试无意义


@dataclass
class CallResult:
    """外部调用的三态结果: 让调用方区分"真没数据"和"调用挂了"

    `EMPTY` 与 `DEGRADED` 的区别是本模块存在的主要理由 —— 旧代码里两者都是 `[]`。
    """

    status: CallStatus
    value: Any = None
    reason: str = ""
    error: Optional[AppError] = None
    attempts: int = 0
    elapsed: float = 0.0
    meta: dict = field(default_factory=dict)

    # ---- 构造函数 ----
    @classmethod
    def of(cls, value: Any, *, attempts: int = 1, elapsed: float = 0.0) -> "CallResult":
        """按返回值是否为空自动判定 OK / EMPTY(空容器与 None 视作 EMPTY)"""
        empty = value is None or (
            isinstance(value, (list, tuple, set, dict, str)) and len(value) == 0
        )
        return cls(
            status=CallStatus.EMPTY if empty else CallStatus.OK,
            value=value,
            reason="无数据" if empty else "",
            attempts=attempts,
            elapsed=elapsed,
        )

    @classmethod
    def disabled(cls, reason: str, *, attempts: int = 0,
                 elapsed: float = 0.0) -> "CallResult":
        """能力未配置: 刻意降级, 不记 error(它不是故障)

        attempts 默认 0 是"根本没调用"(直接构造 DISABLED); 若这次 DISABLED 是
        `call_external` 里真调了一次才抛出来的, 它会把 attempts 传进来 —— 失败
        次数要如实记, 否则"为什么这维没数据"在日志里看不出是没试过还是试了不行。
        """
        return cls(status=CallStatus.DISABLED, value=None, reason=reason,
                   attempts=attempts, elapsed=elapsed)

    @classmethod
    def degraded(cls, error: AppError, *, attempts: int = 0,
                 elapsed: float = 0.0, reason: str = "") -> "CallResult":
        return cls(status=CallStatus.DEGRADED, value=None,
                   reason=reason or error.user_message, error=error,
                   attempts=attempts, elapsed=elapsed)

    @classmethod
    def failed(cls, error: AppError, *, attempts: int = 0,
               elapsed: float = 0.0, reason: str = "") -> "CallResult":
        return cls(status=CallStatus.FAILED, value=None,
                   reason=reason or error.user_message, error=error,
                   attempts=attempts, elapsed=elapsed)

    # ---- 便捷判定 ----
    @property
    def has_data(self) -> bool:
        """拿到了非空数据"""
        return self.status is CallStatus.OK

    @property
    def ok(self) -> bool:
        """调用本身是成功的(EMPTY 也算成功, 只是没数据; DISABLED 是没调用)

        注意与 `usable` 的区别: 能力未配置时 ok=False 但 usable=True ——
        流程可以继续, 只是这一维没有数据。
        """
        return self.status in (CallStatus.OK, CallStatus.EMPTY)

    @property
    def usable(self) -> bool:
        """能不能拿这个结果继续跑

        三种情况都能继续: 有数据 / 真没数据 / 该能力未配置(刻意降级)。
        只有 DEGRADED、FAILED 才是"这条路出了故障"。
        """
        return self.status in (CallStatus.OK, CallStatus.EMPTY, CallStatus.DISABLED)

    @property
    def is_degraded(self) -> bool:
        """这一路降级或失败了 —— 需要显式告诉用户, 不能静默

        与 `usable` 配合使用: usable=False 才该走失败分支;
        DISABLED 不算降级(它是刻意的), 但 `as_note` 里仍会写明"未启用"。
        """
        return self.status in (CallStatus.DEGRADED, CallStatus.FAILED)

    def as_note(self, label: str) -> str:
        """组装成写进 steps_log / 响应里的"这一路怎么了"说明"""
        if self.status is CallStatus.OK:
            return f"{label}: 正常({self._size()})"
        if self.status is CallStatus.EMPTY:
            return f"{label}: 无数据"
        if self.status is CallStatus.DISABLED:
            return f"{label}: 未启用({self.reason})"
        return f"{label}: 降级({self.reason})"

    def _size(self) -> str:
        if isinstance(self.value, (list, tuple, set)):
            return f"{len(self.value)} 条"
        if isinstance(self.value, dict):
            return f"{len(self.value)} 字段"
        return "有值"


# ============================ 统一调用层 ============================


def call_external(
    fn: Callable[[], Any],
    *,
    kind: str = "external",
    retries: int = 2,
    backoff: float = 0.5,
    max_backoff: float = 8.0,
    retry_budget: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> CallResult:
    """调用外部函数, 把失败分类成 `CallResult`(不抛 AppError)

    重试策略(三件套缺一不可):
      - **次数上限** retries
      - **指数退避** backoff * 2^(n-1), 单次不超过 max_backoff
      - **总时长预算** retry_budget —— 从第一次尝试起算; 退避和次数都满足时
        仍可能超总时长, 这是唯一不能省的那件

    只重试 `TransientError`(超时/限流/5xx/连接失败); `PermanentError` 立即
    返回 FAILED; **认不出的异常原样抛出** —— 程序缺陷不该被这里吞掉。

    Args:
        fn:          无参可调用(用 lambda / functools.partial 固定实参)
        kind:        日志与错误里的来源标识(如 "amap.weather")
        retry_budget: 整个重试过程的总时长预算(秒)。单次调用自身的超时由
                      httpx 的 timeout 负责, 这里管的是"重试到什么时候就放弃"
        sleep/clock: 可注入, 便于测试用假时钟验证退避与预算而不真等
    """
    started = clock()
    attempt = 0
    last_err: Optional[AppError] = None

    while True:
        attempt += 1
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001  分类后按需重抛(认不出的向上抛)
            err = classify(exc, kind=kind)
            if err is None:
                # 认不出来 -> 不吞。程序缺陷必须冒出来被人看到。
                raise
            if isinstance(err, CapabilityDisabled):
                # 可选能力未开: 不是故障 —— 立即返回 DISABLED, 不重试、不记 error
                return CallResult.disabled(
                    f"[{kind}] {err}" if kind else str(err),
                    attempts=attempt, elapsed=clock() - started,
                )
            last_err = err
            if not err.retryable or attempt > retries:
                break
            delay = min(backoff * (2 ** (attempt - 1)), max_backoff)
            if clock() - started + delay > retry_budget:
                last_err = TransientError(
                    f"[{kind}] 重试预算耗尽({retry_budget:.1f}s, 已试 {attempt} 次): {err}",
                    code="retry_budget_exhausted",
                )
                break
            # 纯 ASCII 前缀: 本机的控制台是 GBK, 带 emoji 的 print 会抛
            # UnicodeEncodeError(实测), 等于给重试路径装了个新的崩溃点
            print(f"[retry] [{kind}] 第 {attempt} 次失败({err.code}), "
                  f"{delay:.2f}s 后重试: {err}")
            sleep(delay)
        else:
            return CallResult.of(value, attempts=attempt, elapsed=clock() - started)

    elapsed = clock() - started
    if last_err is not None and last_err.retryable:
        return CallResult.degraded(last_err, attempts=attempt, elapsed=elapsed)
    return CallResult.failed(last_err or AppError("未知失败"), attempts=attempt,
                             elapsed=elapsed)


def disabled(reason: str) -> CallResult:
    """"这项能力没配置"的显式表达(如未设置 BOCHA_API_KEY)

    旧代码里"没配 key"和"接口挂了"都是 `[]`; 现在它是 DISABLED ——
    刻意的能力降级, 不是故障, 也不该被当成错误上报。
    """
    return CallResult.disabled(reason)