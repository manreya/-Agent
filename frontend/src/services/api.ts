// 团建活动策划 API 层：与 backend/app/api/routes/event.py 对齐。
// 旅行规划接口(/api/trip/*)已下线，本文件仅保留团建五接口 + 健康检查。
import axios, { AxiosError } from 'axios'
import type {
  EventParseBody,
  EventRequest,
  PlanStartBody,
  PlanStartResponse,
  ResumeBody,
} from '@/types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || ''

const apiClient = axios.create({
  baseURL: API_BASE_URL, // 默认走 vite proxy /api -> localhost:8000
  timeout: 300000, // /plan、/resume(否决重走) 是长请求：四路LLM并行+回炉, 实测1~3分钟
  headers: { 'Content-Type': 'application/json' },
})

/** 400 时后端在 detail 里带 missing_fields（驱动表单补全），需要透传 */
export interface MissingFieldsError extends Error {
  missingFields?: string[]
}

function wrapError(error: unknown, fallback: string): Error {
  const err = error as AxiosError<{ detail?: unknown }>
  const detail = err.response?.data?.detail
  if (err.response?.status === 400 && detail) {
    const e: MissingFieldsError = new Error(
      typeof detail === 'string' ? detail : (detail as { message?: string })?.message || fallback,
    )
    if (typeof detail === 'object' && detail !== null) {
      e.missingFields = (detail as { missing_fields?: string[] }).missing_fields
    }
    return e
  }
  // 422 是 FastAPI 的 schema 校验失败(detail 是数组), 直接 String() 会得到
  // "[object Object]" —— 提到"哪个字段为什么不合格", 不然用户只看到一句
  // "Request failed with status code 422"。
  if (err.response?.status === 422 && Array.isArray(detail)) {
    const parts = detail.map((d) => {
      const item = d as { loc?: unknown[]; msg?: string }
      const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : '?'
      return `${String(field)}: ${item.msg || '不合法'}`
    })
    return new Error(`${fallback}：${parts.join('；')}`)
  }
  return new Error(
    (typeof detail === 'string' && detail) || err.message || fallback,
  )
}

/** 需求解析：raw_text -> EventRequest；缺人数/日期抛 400（missingFields 在 error.missingFields） */
export async function parseRequest(body: EventParseBody): Promise<EventRequest> {
  try {
    const { data } = await apiClient.post<EventRequest>('/api/event/parse', body)
    return data
  } catch (e) {
    throw wrapError(e, '需求解析失败')
  }
}

/** 发起完整策划流程：跑到审批闸门挂起，返回 thread_id + approval_payload */
export async function startPlan(body: PlanStartBody): Promise<PlanStartResponse> {
  try {
    const { data } = await apiClient.post<PlanStartResponse>('/api/event/plan', body)
    return data
  } catch (e) {
    throw wrapError(e, '发起策划流程失败')
  }
}

/** 提交审批结果：approved=true 走完拿 checklist；false+feedback 重走后再次暂停 */
export async function resumeApproval(body: ResumeBody): Promise<PlanStartResponse> {
  try {
    const { data } = await apiClient.post<PlanStartResponse>('/api/event/resume', body)
    return data
  } catch (e) {
    throw wrapError(e, '提交审批结果失败')
  }
}

/** 健康检查（节点清单/持久化路径） */
export async function healthCheck(): Promise<Record<string, unknown>> {
  const { data } = await apiClient.get('/api/event/health')
  return data
}

export default apiClient
