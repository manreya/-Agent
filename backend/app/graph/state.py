"""LangGraph 状态定义(多人团建活动策划)"""

import operator
from typing import Annotated, List, Optional
from typing_extensions import TypedDict


from ..models.schemas import PrefSummary


def merge_list(left: Optional[List], right: Optional[List]) -> List:
    """列表合并 reducer: None 安全拼接(供后续 Phase 并行采集字段使用)"""
    return list(left or []) + list(right or [])


def merge_dict(left: Optional[dict], right: Optional[dict]) -> dict:
    """字典按 key 合并 reducer: 四路提案节点各写不同 key, 并行写入自动合拢
    (写法参考 merge_list; 同 key 后写覆盖前写)"""
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class EventPlanState(TypedDict, total=False):
    """团建活动策划图的共享状态

    并行采集节点后续将分别写 venues / weather / route_info 独立 key,
    LangGraph 对不同 key 的并行写入天然安全。
    """

    # ---- 输入 ----
    raw_request: dict                       # 原始输入, 如 {"raw_text": "10月底组织30人团建..."}

    # ---- 需求解析(parse_request 节点产出) ----
    parsed_request: dict                    # EventRequest.model_dump()(结构化抽取结果)
    activity_type: Optional[str]            # 活动类型分类(团建/生日会/...)
    missing_fields: List[str]               # 缺失关键字段(人数/日期缺失时路由层返回 400)

    # ---- 团队历史记忆(parse_request 读 Store 产出) ----
    # {"profile": {...}, "retros": [...]}, 无记录/无 Store 时为 None。
    # 单独一个字段, **不并进 parsed_request**: 那是"本次需求"的字段, 把上次
    # 活动的记录塞进去, 读的人就再也分不清"用户这次说的"和"上次留下的"。
    # 结构化路径没有 LLM 提示词可注入, 它读出来是给审批页展示、给人做参照的。
    team_history: Optional[dict]

    # ---- 成员偏好(前端可分批提交, operator.add 自动累加) ----
    preferences: Annotated[List, operator.add]   # List[PersonPref], 多批写入自动拼接
    pref_summary: Optional[PrefSummary]          # aggregate_prefs 节点产出的汇总

    # ---- 基础信息(gather_info 节点产出, 节点内部五路并行采集) ----
    basic_info: Optional[dict]                   # BasicInfo.model_dump()(venues/weather/route_info)

    # ---- 数据可用性(gather_info 节点产出) ----
    # 每路采集的结局: {"venues": {"status": "ok|empty|disabled|degraded|failed",
    #                             "reason": "...", "attempts": 1, "elapsed": 0.4}, ...}
    # 存在的理由: 采集失败过去只有两种表达 —— 静默空列表 or 整节点崩。前者让
    # "今天杭州真没火车"和"聚合数据挂了"长得一模一样, 下游无从分辩, 只能把
    # 缺失当常态继续产出方案。有了这张表, "哪一维降级了、为什么"是可查的,
    # 也是对外必须说明的部分(不能拿降级后的空数据冒充"已核实")。
    # 降级维度不另设字段, 一律从本表派生 —— 两个真相来源迟早会不一致。
    data_status: Annotated[dict, merge_dict]

    # ---- 四路提案(propose 派发的 4 个子任务并行产出, merge_dict 按 key 合拢) ----
    proposals: Annotated[dict, merge_dict]       # {"transport": {...}, "venue": {...}, "flow": {...}, "food": {...}}

    # ---- 约束消解(reconcile 节点产出, 纯程序零LLM) ----
    conflict_report: Optional[dict]              # ConflictReport.model_dump()(通过校验时 conflicts 为空)
    replan_count: int                            # 检出问题即自增; > MAX_REPLAN(2) 不再回炉, 交人工裁决(HITL)

    # ---- 风险评估(risk 节点产出: 规则层纯Python, 有标记才追加一次LLM备选) ----
    # RiskReport.model_dump(): level(low/medium/high/unknown) / flags(结构化: id/rule/
    # severity/detail/evidence) / rules(每条规则 命中|清白|未评估, 免得"没评估"被读成
    # "没事") / alternatives / uncovered_flags / unevaluated_inputs / summary。
    # 规则层是纯函数(免费), 交付前与换选后都会重算, 所以这个字段可能被覆盖成基于
    # **最终**方案的结论 —— 这是有意的, 不是状态被谁改脏了。
    risk_report: Optional[dict]

    # ---- 人工审批 HITL(approval 节点, interrupt 暂停等待 /api/event/resume) ----
    approval_payload: Optional[dict]             # 审批页数据: 最终方案/预算明细/分歧点/风险备选
    approval_decision: Optional[dict]            # resume 写回: {"approved": bool, "feedback": str, "selections": dict}
    approval_selections: Optional[dict]          # 审批页多候选挑选结果: {"food": "候选名", "transport": "高铁"}
    user_feedback: Annotated[List[str], operator.add]  # 历次否决意见/换选意见(approved=False 时累加, 注入重生成 prompt)

    # ---- 收尾(checklist/memory 节点产出, 审批通过后执行) ----
    checklist: Optional[dict]                    # TaskChecklist.model_dump()(分工/采购/通知文案)
    # 团队记忆写没写成(可能因存储故障降级放行)。交付页说"已记入团队档案"必须
    # 以它为准, 不能假定成功(success 分支与降级分支的返回体一模一样时, 页面只能猜)
    memory_status: Optional[dict]
    team_id: Optional[str]                       # 团队隔离 ID(Store namespace=("team", team_id))

    # ---- 流程门控(输入标志): 是否进入提案阶段(/parse 不进, /plan 进) ----
    generate_plan: bool

    # ---- 可观测 ----
    steps_log: Annotated[List[str], operator.add]   # 节点执行日志
