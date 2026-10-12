<template>
  <a-card>
    <a-result status="info" title="AI 正在为你生成方案…" :sub-title="currentPhaseText">
      <template #icon>
        <loading-outlined style="font-size: 48px; color: #0f3d2e" />
      </template>
      <template #extra>
        <a-steps :current="phase" direction="vertical" size="small" style="max-width: 420px; margin: 0 auto; text-align: left">
          <a-step v-for="(t, i) in PHASES" :key="i" :title="t" :status="stepStatus(i)" />
        </a-steps>
        <a-alert
          style="margin-top: 16px; text-align: left"
          type="info"
          show-icon
          message="四路 LLM 并行报价 + 预算校验回炉，通常需要 1~3 分钟，请勿关闭页面"
        />
        <a-alert
          v-if="store.error"
          style="margin-top: 12px; text-align: left"
          type="error"
          show-icon
          :message="store.error"
        />
        <a-button v-if="store.status === 'failed'" type="primary" style="margin-top: 12px" @click="$router.push('/')">
          返回重试
        </a-button>
      </template>
    </a-result>
  </a-card>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { LoadingOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import { useEventFlowStore } from '@/stores/eventFlow'

const PHASES = [
  '组装需求（表单结构化字段，零 LLM）',
  '聚合成员偏好',
  '采集场地 / 天气 / 路线（高德）',
  '四路并行报价（交通/场地/流程/餐饮）',
  '预算与时间线校验（超支自动回炉）',
  '风险评估，等待你的审批',
]

const store = useEventFlowStore()
const router = useRouter()

const phase = ref(0)
let timer: ReturnType<typeof setInterval> | null = null

const currentPhaseText = computed(() => `当前阶段：${PHASES[Math.min(phase.value, PHASES.length - 1)]}`)

function stepStatus(i: number): 'wait' | 'process' | 'finish' {
  if (i < phase.value) return 'finish'
  if (i === phase.value) return 'process'
  return 'wait'
}

onMounted(async () => {
  // 分阶段节奏动画（真进度流 SSE 留待后端增强）
  timer = setInterval(() => {
    if (phase.value < PHASES.length - 1) phase.value += 1
  }, 15000)

  // 唯一入口：首页表单发起(pendingLaunch, 结构化需求 + 偏好一次提交)。
  // 原先的 ?raw= 兜底已去掉 —— /plan 现在收结构化入参, 一句裸文本发过去
  // 只会 422; 自由文本解析是 /parse 的职责, 不复用这条发起路径。
  const pending = store.pendingLaunch
  if (pending) {
    store.pendingLaunch = null
    try {
      await store.launch(pending.payload)
    } catch {
      return // store.error 已展示
    }
  } else if (store.status === 'generating') {
    // status=generating 是从 localStorage 恢复出来的，但本次并没有请求在跑
    // （pendingLaunch 不落盘）——再等下去就是永久转圈。
    store.status = 'failed'
    store.error = '上一次生成已中断（页面被刷新或关闭）。请返回首页重新发起，'
      + (store.threadId ? '或回到审批现场继续未完成的审批。' : '')
    store.persist()
    return
  }
  // 流程结束后按状态跳转
  watchStatusAndRoute()
})

function watchStatusAndRoute() {
  const stop = setInterval(() => {
    if (store.status === 'pending_approval') {
      clearInterval(stop)
      message.success('方案已生成，请审批')
      router.push('/approval')
    } else if (store.status === 'completed') {
      clearInterval(stop)
      router.push('/plan')
    }
  }, 500)
}

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>
