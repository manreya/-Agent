<template>
  <div v-if="plan">
    <a-result
      status="success"
      title="方案已定稿"
      :sub-title="`thread: ${store.threadId.slice(0, 8)}… · ${memoryNote}`"
    />

    <a-alert
      v-if="memoryStatus && !memoryStatus.written"
      type="warning"
      show-icon
      style="margin-bottom: 16px"
      message="本次计划未记入团队档案"
      :description="memoryFailNote"
    />

    <a-row :gutter="16">
      <a-col v-for="p in proposals" :key="p.category" :span="12" style="margin-bottom: 16px">
        <ProposalCard :proposal="p" :category="p.category" />
      </a-col>
    </a-row>

    <a-alert
      v-if="plan.risk_report"
      type="info"
      show-icon
      style="margin-bottom: 16px"
      message="校验与风险结论"
      :description="plan.risk_report"
    />

    <ChecklistBoard v-if="checklist" :checklist="checklist" style="margin-bottom: 16px" />
    <a-alert
      v-else
      type="warning"
      show-icon
      message="未拿到任务清单（checklist），请查看服务日志"
      style="margin-bottom: 16px"
    />

    <a-space>
      <a-button @click="startNew">策划下一场活动</a-button>
    </a-space>
  </div>

  <a-result v-else status="404" title="还没有定稿方案" sub-title="完成审批流程后这里会展示最终方案与执行清单">
    <template #extra>
      <a-button type="primary" @click="$router.push('/')">回首页</a-button>
    </template>
  </a-result>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useEventFlowStore } from '@/stores/eventFlow'
import ProposalCard from '@/components/ProposalCard.vue'
import ChecklistBoard from '@/components/ChecklistBoard.vue'
import type { MemoryStatus, TaskChecklist } from '@/types'

const store = useEventFlowStore()
const router = useRouter()

const plan = computed(() => store.finalPlan)
const proposals = computed(() => plan.value?.proposals ?? [])
const checklist = computed<TaskChecklist | null>(() => {
  const raw = plan.value?.checklist
  if (!raw || typeof raw !== 'object') return null
  const c = raw as Record<string, unknown>
  if (!Array.isArray(c.assignments) || !Array.isArray(c.purchase_list)) return null
  return c as unknown as TaskChecklist
})

// 存档与否由后端那一步给出结论（它可能降级放行），页面照抄，不做假定：
// 一句话说反了，用户下次找不到这次的记录，还会以为系统记得
const memoryStatus = computed<MemoryStatus | null>(() => {
  const raw = plan.value?.memory_status
  if (!raw || typeof raw !== 'object') return null
  return raw as unknown as MemoryStatus
})

const memoryNote = computed(() => {
  const m = memoryStatus.value
  if (!m) return '团队档案状态未知'      // 老响应里没有这个字段，不替它下结论
  return m.written ? '本次计划已记入团队档案，下次策划会在审批页带出来' : '本次计划未能记入团队档案'
})

const memoryFailNote = computed(() => {
  const m = memoryStatus.value
  const why = m?.reason === 'no_store' ? '当前没有可用的存储' : `写入报错：${m?.error || '未知原因'}`
  return `${why}。方案本身不受影响，可正常执行；联系维护者时请提供上面这行 thread。`
})

function startNew() {
  store.reset()
  router.push('/')
}
</script>
