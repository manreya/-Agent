// 流程会话 store：两段式 HITL 的前端状态机（setup 风格）。
// thread_id + 审批轮次快照持久化到 localStorage —— 后端 checkpoint 杀进程可恢复，
// 前端刷新/关页后凭快照恢复审批现场（payload 仅作展示，resume 只依赖 thread_id）。
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type {
  ApprovalPayload,
  ApprovalRound,
  EventPlan,
  FlowStatus,
  PlanStartBody,
  Selections,
  SessionSnapshot,
} from '@/types'
import { resumeApproval, startPlan } from '@/services/api'

const STORAGE_KEY = 'eventflow.session'

function loadSnapshot(): SessionSnapshot | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as SessionSnapshot) : null
  } catch {
    return null
  }
}

export const useEventFlowStore = defineStore('eventFlow', () => {
  // ---- state（从 localStorage 快照恢复）----
  const snap = loadSnapshot()
  const teamId = ref(snap?.teamId ?? '')
  const status = ref<FlowStatus>(snap?.status ?? 'idle')
  const threadId = ref(snap?.threadId ?? '')
  const rounds = ref<ApprovalRound[]>(snap?.rounds ?? [])
  const finalPlan = ref<EventPlan | null>(null)
  const error = ref('')
  /** 表单一次填写模式的待发起点(由 Intake 写入, Generating 消费后清空)
   *  携带**结构化**请求体: 表单已有精确字段, 不再拼成句子让后端 LLM 重抽 */
  const pendingLaunch = ref<{ payload: PlanStartBody } | null>(null)

  // ---- getters ----
  const currentPayload = computed<ApprovalPayload | null>(() =>
    rounds.value.length ? rounds.value[rounds.value.length - 1].payload : null,
  )
  /** 历次否决意见（审批页展示“AI 已按上轮意见修改”上下文） */
  const feedbackHistory = computed<string[]>(() =>
    rounds.value.filter((r) => r.feedback).map((r) => r.feedback as string),
  )
  const hasRestorableSession = computed(
    () => !!threadId.value && status.value === 'pending_approval',
  )

  // ---- actions ----
  function persist() {
    const snapshot: SessionSnapshot = {
      threadId: threadId.value,
      teamId: teamId.value,
      status: status.value,
      rounds: rounds.value,
      savedAt: new Date().toISOString(),
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot))
  }

  function reset() {
    teamId.value = ''
    status.value = 'idle'
    threadId.value = ''
    rounds.value = []
    finalPlan.value = null
    error.value = ''
    localStorage.removeItem(STORAGE_KEY)
  }

  /** 发起完整流程：成功后进入 pending_approval，等待人工审批
   *  payload 为**结构化**请求体(人数/日期/出发地必填, 预算=总预算);
   *  偏好随同提交, 无需再走一轮 /preferences;
   *  team_id 缺省时后端生成不透明 id 并回传, 存下来供后续策划复用同一团队记忆 */
  async function launch(payload: PlanStartBody) {
    status.value = 'generating'
    error.value = ''
    try {
      const resp = await startPlan({ ...payload, team_id: teamId.value || null })
      threadId.value = resp.thread_id
      if (resp.team_id && resp.team_id !== teamId.value) {
        teamId.value = resp.team_id   // 后端归一后的值(可能是它生成的)
      }
      applyResponse(resp)
    } catch (e) {
      status.value = 'failed'
      error.value = (e as Error).message
      throw e
    } finally {
      persist()
    }
  }

  /** 提交审批决策：通过 -> completed+checklist；否决 -> 新一轮 pending_approval
   *  selections: 审批页多候选挑选结果（交通等="候选名"；餐饮逐顿={"D1|午餐":"候选餐厅名"}） */
  async function decide(approved: boolean, feedback?: string, selections?: Selections) {
    if (!threadId.value) throw new Error('缺少 thread_id，无法恢复流程')
    const last = rounds.value[rounds.value.length - 1]
    status.value = 'generating'
    error.value = ''
    try {
      const resp = await resumeApproval({
        thread_id: threadId.value,
        approved,
        feedback: approved ? null : feedback || null,
        selections: selections && Object.keys(selections).length ? selections : undefined,
      })
      // 决策只在**拿到响应之后**才写进本轮记录：先写后发等于把"用户已通过"
      // 记成一个尚未发生的事实 —— 请求失败时，本地历史会显示"已通过"，
      // 而服务端那次审批根本没跑完。
      if (last) {
        last.decision = approved ? 'approved' : 'rejected'
        if (!approved && feedback) last.feedback = feedback
        if (selections && Object.keys(selections).length) last.selections = selections
      }
      applyResponse(resp)
    } catch (e) {
      // 失败必须保持"可恢复"：后端在最后一步失败时，线程仍停在那个节点上，
      // 再提交一次同一个请求就能接着跑完（审批不会被重问）。前端若把状态置成
      // failed，可恢复会话的判断（要求 pending_approval）就再也进不来 ——
      // 用户一刷新就只能从头再策划一遍，而服务端一切完好。
      status.value = 'pending_approval'
      error.value = (e as Error).message
      throw e
    } finally {
      persist()
    }
  }

  /** 统一处理 /plan 与 /resume 响应 */
  function applyResponse(resp: {
    status: string
    approval_payload?: unknown
    data?: EventPlan | null
    message?: string
  }) {
    if (resp.status === 'pending_approval' && resp.approval_payload) {
      rounds.value.push({
        round: rounds.value.length + 1,
        payload: resp.approval_payload as ApprovalPayload,
        decision: null,
      })
      status.value = 'pending_approval'
    } else if (resp.status === 'completed') {
      finalPlan.value = resp.data ?? null
      status.value = 'completed'
    } else {
      status.value = 'failed'
      error.value = resp.message || '未知流程状态'
    }
  }

  return {
    teamId,
    status,
    threadId,
    rounds,
    finalPlan,
    error,
    pendingLaunch,
    currentPayload,
    feedbackHistory,
    hasRestorableSession,
    persist,
    reset,
    launch,
    decide,
    applyResponse,
  }
})
