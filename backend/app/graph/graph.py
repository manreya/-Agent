"""LangGraph 图组装(多人团建活动策划)

完整流程(generate_plan=True, 如 /plan):
    START -> parse_request (结构化输入零 LLM 直接组装; 自由文本才走 LLM 抽取,
                             team_id 有历史时注入"上次你们……"上下文)
          -> aggregate_prefs (偏好聚合)
          -> gather_info (六路并行采集, 零LLM)
          -> [Send 派发] -> propose_transport ┐
                        -> propose_venue     ├ 四路并行 LLM 提案
                        -> propose_flow      │
                        -> propose_food      ┘ (唯一绑定受控搜索工具)
          -> reconcile (约束消解: 预算/时间线/容量, 纯程序零LLM)
          -> 条件回环:
               未通过 且 replan_count <= MAX_REPLAN(2)
                   -> [Send 重新派发] 四路(ConflictReport 注入 prompt, 带具体数字回去改)
               通过 / replan_count > MAX_REPLAN
                   -> risk (风险评估)
          -> risk (规则层纯Python: 雨天+室外/强度vs体能; 有标记才调一次LLM备选)
          -> approval (HITL interrupt 暂停, payload=方案+预算+分歧点+风险备选)
               approved=True  -> checklist (任务清单: 分工/采购/通知文案, 一次LLM)
                             -> memory (团队记忆写入 Store: 画像+复盘标签) -> END
                 TODO: 通知/付款等不可逆动作前, 在 checklist 之前插入第二个 interrupt 节点(占位待实现)
               approved=False -> aggregate_prefs 重走(user_feedback 注入四路 prompt)
    轻量流程(generate_plan=False, 如 /parse 与 evals):
        START -> parse_request -> aggregate_prefs -> gather_info -> END
                 (dispatch 返回空列表, 不进入提案阶段, 省 4 次 LLM)

    入口唯一: START 无条件进 parse_request。历史上曾有"仅偏好提交"直达
    aggregate_prefs 的入口(/preferences 端点), 该端点已下线, 且 /plan 必带
    structured、evals 必带 raw_text, 该分支不可达 —— 故整条入口路由已删除;
    缺 raw_text/structured 的调用会由 parse_request 以 missing_fields=["raw_text"]
    明确报错, 而不是静默绕过解析。

注意:
- 回环图必须在 ainvoke 时设置 recursion_limit(如 40), 不设上限会烧钱。
- HITL 需 checkpointer(AsyncSqliteSaver, backend/data/checkpoints.db);
  无 checkpointer 时 interrupt 仍会暂停, 但状态不落盘, 无法跨进程 resume。
- 团队记忆需 store(langgraph Store, backend/data/memory.db); 不传时
  parse_request 不注入历史、memory 节点优雅跳过, 图功能不受影响。
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph, START, END
from langgraph.store.base import BaseStore

from .nodes import (
    aggregate_prefs, approval, assess_risk, checklist, dispatch_proposals,
    gather_info, memory, parse_request, propose_food, propose_flow,
    propose_transport, propose_venue, reconcile, route_after_approval,
    route_after_reconcile,
)
from .state import EventPlanState

# 四路提案节点注册表(key 与 Proposal.category 对应)
_PROPOSE_NODES = {
    "propose_transport": propose_transport,
    "propose_venue": propose_venue,
    "propose_flow": propose_flow,
    "propose_food": propose_food,
}


def build_event_graph(
    checkpointer: BaseCheckpointSaver = None,
    store: BaseStore = None,
):
    """构建团建活动策划 StateGraph 并编译

    checkpointer: HITL 需要传 AsyncSqliteSaver(见 routes/event.py);
                  None 时不落盘(轻量流程/单测用)。
    store:        团队记忆(langgraph Store, 见 routes/event.py); None 时
                  parse_request 不注入历史上下文、memory 节点跳过写入。
    """
    builder = StateGraph(EventPlanState)

    builder.add_node("parse_request", parse_request)
    builder.add_node("aggregate_prefs", aggregate_prefs)
    builder.add_node("gather_info", gather_info)
    for name, fn in _PROPOSE_NODES.items():
        builder.add_node(name, fn)
    builder.add_node("reconcile", reconcile)
    builder.add_node("risk", assess_risk)
    builder.add_node("approval", approval)
    builder.add_node("checklist", checklist)
    builder.add_node("memory", memory)

    # 入口唯一: 无需求输入时 parse_request 自己报 missing_fields, 不走旁路
    builder.add_edge(START, "parse_request")
    builder.add_edge("parse_request", "aggregate_prefs")
    builder.add_edge("aggregate_prefs", "gather_info")

    # Send 派发: dispatch 返回 List[Send] 时四路并行执行, 返回 [] 时流程自然结束
    builder.add_conditional_edges("gather_info", dispatch_proposals)

    # 四路提案汇入约束消解, 条件回环(重新派发也走 Send, 复用并行拓扑);
    # 放行(通过/超上限)时 route 返回 "risk" 进入风险评估
    for name in _PROPOSE_NODES:
        builder.add_edge(name, "reconcile")
    builder.add_conditional_edges("reconcile", route_after_reconcile)

    # 风险评估 -> 人工审批(HITL interrupt); 通过->checklist->memory->END, 否决->重走聚合
    builder.add_edge("risk", "approval")
    builder.add_conditional_edges(
        "approval", route_after_approval,
        {"checklist": "checklist", "aggregate_prefs": "aggregate_prefs"},
    )
    # TODO: 通知/付款等不可逆动作前, 在此插入第二个 interrupt 节点(占位待实现)
    builder.add_edge("checklist", "memory")
    builder.add_edge("memory", END)

    return builder.compile(checkpointer=checkpointer, store=store)
