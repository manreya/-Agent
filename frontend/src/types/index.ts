// 业务类型：由 openapi-typescript 生成的 src/types/api.d.ts 取别名，
// 禁止手写漂移；后端 schema 变更后执行 npm run gen-types 重新生成。
import type { components } from './api'

type S = components['schemas']

// ---- 后端契约别名 ----
export type EventRequest = S['EventRequest']
export type EventParseBody = S['EventParseBody']
export type PlanStartBody = S['PlanStartBody']
export type PlanStartResponse = S['PlanStartResponse']
export type ResumeBody = S['ResumeBody']
export type EventPlan = S['EventPlan']
export type Proposal = S['Proposal']
export type ProposalItem = S['ProposalItem']
export type ProposalOption = S['ProposalOption']
export type MealOptions = S['MealOptions']
export type PersonPref = S['PersonPref']
export type PlaceInput = S['PlaceInput']
// PrefSummary 已无端点导出(/preferences 下线, 它只剩图内部状态字段);
// 前端需要的部分(conflict_points)已由下方手写的 ApprovalPayload 覆盖

export type ProposalCategory = Proposal['category']

// RiskReport/ConflictReport 未出现在任何端点响应模型里（EventPlan.risk_report 是 str,
// approval_payload 是 dict），OpenAPI 未导出——按 backend/app/models/schemas.py 手动对齐：
export interface RiskReportModel {
  /**
   * unknown = 所有规则都没能评估（缺天气/没人填体能…）。它与 low 必须分开显示：
   * 两者过去都表现为"没有标记 + 绿色标签"，于是"没查"被读成"没事"。
   */
  level: 'low' | 'medium' | 'high' | 'unknown'
  flags: RiskFlagModel[]
  /** 每条规则的执行情况：命中/清白/未评估（没评估的必须能一眼看到，含原因） */
  rules: RuleStatusModel[]
  alternatives: RiskAlternative[]
  /** 没有被任何备选覆盖的标记 id（只在 summary 里说"已给出N条"是不够的） */
  uncovered_flags?: string[]
  /** 强度/室内外解析不出的项目名（规则的盲区） */
  unevaluated_inputs?: string[]
  summary: string
}

export interface RiskFlagModel {
  /** 稳定标识，备选按它关联（不再是回抄原文） */
  id: string
  rule: string
  severity: 'medium' | 'high'
  detail: string
  day?: string | null
  evidence?: Record<string, unknown>
}

export interface RuleStatusModel {
  rule: string
  name?: string
  status: 'hit' | 'clear' | 'not_evaluated'
  reason?: string
  detail?: string
}

/** 审批前还没闭合的事（与风险等级分开：等级答"方案多大风险"，它答"还有哪些没闭合"） */
export interface BlockingIssue {
  kind: 'conflict' | 'budget' | 'data' | 'risk_gap'
  source: string
  detail: string
}

export interface ConflictReportModel {
  /** 超预算金额(元), 负数=结余 */
  over_budget: number
  conflicts: string[]
  suggestions: string[]
  /**
   * 冲突归属（类别 -> 冲突原文）；未列入任何类别的视为**全局冲突**（如预算）。
   * 后端用它做两件事：定向回炉（只重派相关的那几路）与给每路 prompt 分流冲突。
   * 审批页不需要展示它（`conflicts` 已经是原文），保留只为与后端模型对齐。
   */
  conflicts_by_category?: Record<string, string[]>
}

// ---- 审批 payload（interrupt 推出，后端 dict 非 pydantic，UI 侧定义，与
//      backend/app/graph/nodes.py _build_approval_payload 保持同步）----
export interface BudgetDetail {
  total: number
  by_category: Record<string, number>
  budget_limit: number | null
  /** 正数=超支, 负数=结余（与后端约定一致） */
  over_budget: number | null
}

export interface RiskAlternative {
  /** 经后端校验确实存在的标记 id（覆盖不到的备选已被丢弃） */
  covers?: string[]
  plan: string
  est_cost?: number
  /** 费用是否为模型估算（后端填）：是就必须在界面上标出来 */
  est_cost_is_estimate?: boolean
  /** 是否已计入预算合计（后端填）：备选一律 false，免得读成已扣款 */
  counted_in_total?: boolean
}

export interface ApprovalPayload {
  proposals: Partial<Record<ProposalCategory, Proposal>>
  budget: BudgetDetail
  conflict_points: string[]
  risk_report: Partial<RiskReportModel> & { alternatives?: RiskAlternative[] }
  conflict_report: Partial<ConflictReportModel>
  user_feedback: string[]
  /** 阻断项：未解冲突/超预算/采集降级/规则未评估，置顶展示 */
  blocking?: BlockingIssue[]
  /** 该团队过往活动的记录（有则展示，无则整块不出现） */
  team_history?: TeamHistory | null
}

// ---- checklist（EventPlan.checklist 为 dict，UI 侧结构化，与
//      backend/app/models/schemas.py TaskChecklist 保持同步）----
export interface Assignment {
  role: string
  owner?: string | null
  detail?: string | null
}

export interface PurchaseItem {
  item: string
  quantity?: number | null
  est_cost?: number | null
  note?: string | null
  /** 出自哪一路提案（后端规范化，认不出为 null）——决定它能否参与"不超该路报价"的核对 */
  category?: ProposalCategory | null
  /** 费用是模型估算（后端填）：清单里的钱从没被询过价，必须标出来 */
  est_cost_is_estimate?: boolean
}

export interface TaskChecklist {
  assignments: Assignment[]
  purchase_list: PurchaseItem[]
  notice_text: string
  /** 模型生成失败时由程序按已批准方案拼的兜底清单（内容仍只含方案里的事实） */
  degraded?: boolean
  /** 程序校验发现的问题（超该路报价/归属认不出/清单为空） */
  warnings?: string[]
}

// ---- 团队历史记忆（parse 阶段从 Store 读出，审批页展示用；不参与任何判定）----
export interface TeamHistoryProfile {
  people_count?: number | null
  fitness_distribution?: Record<string, number>
  dietary_all?: string[]
  activity_votes?: Record<string, number>
  last_activity_date?: string | null
}

export interface TeamHistoryRetro {
  activity_date?: string | null
  /** 计划阶段四路报价合计（活动尚未发生，不是实际花费） */
  plan_total_cost?: number | null
  /** 计划与预算的差（正=超支, 负=结余, 0=正好打平, null=没给预算） */
  plan_budget_deviation?: number | null
  approved?: boolean
  rejected_reasons?: string[]
  replan_count?: number
}

export interface TeamHistory {
  profile?: TeamHistoryProfile | null
  retros?: TeamHistoryRetro[]
}

// ---- 团队记忆写入结果（收尾那一步给出，交付页必须照它说话）----
// 写入可能失败并降级放行（本次策划照常交付，只是没存档）：页面不能假定成功。
export interface MemoryStatus {
  written: boolean
  /** no_store=没有存储可用 / write_failed=写入报错, 成功时为 null */
  reason?: 'no_store' | 'write_failed' | null
  error?: string | null
  team_id?: string | null
  retro_key?: string | null
  activity_date?: string | null
}

// ---- 前端流程会话 ----
export type FlowStatus =
  | 'idle'
  | 'generating'
  | 'pending_approval'
  | 'completed'
  | 'failed'

/** 多候选挑选结果: 交通等单选 = 候选名; 餐饮逐顿挑选 = { "D1|午餐": 候选餐厅名 } */
export type Selections = Record<string, string | Record<string, string>>

/** 一轮审批记录：payload 快照 + 本轮决策（selections=多候选挑选结果） */
export interface ApprovalRound {
  round: number
  payload: ApprovalPayload
  decision: 'approved' | 'rejected' | null
  feedback?: string
  selections?: Selections
}

/** localStorage 持久化快照（杀进程/刷新后恢复审批现场） */
export interface SessionSnapshot {
  threadId: string
  teamId: string
  status: FlowStatus
  rounds: ApprovalRound[]
  savedAt: string
}

export const CATEGORY_LABEL: Record<ProposalCategory, string> = {
  transport: '交通',
  venue: '场地',
  flow: '流程',
  food: '餐饮',
}

export const CATEGORY_ORDER: ProposalCategory[] = ['transport', 'venue', 'flow', 'food']
