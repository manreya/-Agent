<template>
  <div v-if="payload">
    <a-page-header
      :title="`方案审批 · 第 ${store.rounds.length} 版`"
      :sub-title="`thread: ${store.threadId.slice(0, 8)}…`"
    />

    <!-- 否决历史：让用户看到 AI 已按上轮意见修改 -->
    <a-alert
      v-if="store.feedbackHistory.length"
      type="warning"
      show-icon
      style="margin-bottom: 16px"
      message="历史修改意见（已注入本轮重新生成）"
    >
      <template #description>
        <div v-for="(f, i) in store.feedbackHistory" :key="i">第{{ i + 1 }}轮：{{ f }}</div>
      </template>
    </a-alert>

    <BudgetSummary :budget="payload.budget" style="margin-bottom: 16px" />

    <!-- 团队历史（该团队过往活动的记录，有才显示）
         标注"计划报价"而不是"花费"：记录写于审批通过之后、活动发生之前 -->
    <a-card v-if="historyProfile || historyRetros.length" size="small" title="团队历史" style="margin-bottom: 16px">
      <div v-if="historyProfile" style="color: #555">
        <span v-if="historyProfile.last_activity_date" style="color: #999">
          最近一次活动 {{ historyProfile.last_activity_date }} ·
        </span>
        <span v-if="historyProfile.people_count">共 {{ historyProfile.people_count }} 人</span>
        <span v-if="historyProfile.dietary_all?.length">· 忌口：{{ historyProfile.dietary_all.join('、') }}</span>
        <span v-if="historyVotes">· 偏好：{{ historyVotes }}</span>
      </div>
      <div v-for="(r, i) in historyRetros" :key="i" style="margin-top: 6px; color: #555">
        <b>{{ r.activity_date || '更早一次' }}</b>
        <span v-if="r.plan_total_cost != null">· 计划报价 ¥{{ fmt(r.plan_total_cost) }}</span>
        <span v-if="r.plan_budget_deviation != null">
          · {{ deviationText(r.plan_budget_deviation) }}
        </span>
        <span v-if="r.approved === false" style="color: #a8071a">· 当时未经通过</span>
        <div v-if="r.rejected_reasons?.length" style="color: #8c8c8c; font-size: 12px">
          曾否决/回炉：{{ r.rejected_reasons.slice(0, 3).join('；') }}
        </div>
      </div>
      <div style="color: #999; font-size: 12px; margin-top: 6px">
        仅供参照，不会改动本次方案里任何字段
      </div>
    </a-card>

    <!-- 阻断项：与风险等级分开。等级答“方案本身多大风险”，这里答“还有哪些事没闭合”
         （未解冲突/超预算/采集降级/规则未评估）。合并成一个等级会让人无从分辨是哪种 -->
    <a-alert
      v-if="blocking.length"
      type="error"
      show-icon
      style="margin-bottom: 16px"
      message="以下事项尚未闭合，通过即视为接受"
    >
      <template #description>
        <div v-for="(b, i) in blocking" :key="i">· [{{ b.source }}] {{ b.detail }}</div>
      </template>
    </a-alert>

    <a-row :gutter="16">
      <a-col v-for="cat in categoriesWithProposal" :key="cat" :span="12" style="margin-bottom: 16px">
        <ProposalCard
          v-model="selections[cat]"
          :proposal="payload.proposals[cat]!"
          :category="cat"
        />
      </a-col>
    </a-row>

    <a-card v-if="payload.conflict_points.length" size="small" title="成员偏好提示（分歧 / 约束）" style="margin-bottom: 16px">
      <a-tag v-for="(c, i) in payload.conflict_points" :key="i" color="orange" style="margin-bottom: 6px">
        {{ c }}
      </a-tag>
    </a-card>

    <!-- 风险评估与备选 -->
    <a-card size="small" style="margin-bottom: 16px">
      <template #title>
        风险评估
        <a-tag :color="riskColor" style="margin-left: 8px">{{ riskLevelText }}</a-tag>
      </template>
      <p v-if="payload.risk_report.summary" style="color: #555">{{ payload.risk_report.summary }}</p>

      <!-- 规则执行表：每条规则都必须表态。"未评估"必须看得见 —— 它过去和"没事"长得一样，
           都是没有标记 + 绿标签，于是"没查"被读成"没事" -->
      <div v-if="ruleRows.length" style="margin: 8px 0">
        <a-tag
          v-for="r in ruleRows"
          :key="r.rule"
          :color="ruleColor(r.status)"
          style="margin-bottom: 6px"
        >
          {{ r.name || r.rule }}：{{ ruleText(r.status) }}<span v-if="r.reason">（{{ r.reason }}）</span>
        </a-tag>
      </div>

      <!-- 标记逐条展示：显示的是人话详情，不是整个对象 -->
      <a-alert
        v-for="f in flags"
        :key="f.id"
        :type="f.severity === 'high' ? 'error' : 'warning'"
        show-icon
        :message="f.detail"
        style="margin-bottom: 8px"
      />
      <p v-if="uncovered.length" style="color: #a8071a">
        以下风险暂无备选，通过前请人工确认：{{ uncovered.join('；') }}
      </p>
      <p v-if="unevaluated.length" style="color: #8c8c8c">
        以下项目未能识别强度/室内外，未计入判定：{{ unevaluated.join('、') }}
      </p>

      <div v-if="alternatives.length">
        <div style="font-weight: 600; margin: 8px 0">备选方案（可引用到否决意见中）：</div>
        <a-card
          v-for="(alt, i) in alternatives"
          :key="i"
          size="small"
          style="margin-bottom: 8px; background: #f6f8f7"
        >
          <b>备选{{ i + 1 }}</b>：{{ alt.plan }}
          <span v-if="alt.est_cost" style="color: #8c8c8c">
            （{{ alt.est_cost_is_estimate === false ? '约' : '模型估算约' }}¥{{ fmt(alt.est_cost)
            }}{{ alt.counted_in_total ? '' : '，未计入合计' }}）
          </span>
          <a-button size="small" type="link" @click="citeAlternative(i, alt)">引用到意见</a-button>
        </a-card>
      </div>
    </a-card>

    <a-affix :offset-bottom="16">
      <a-card size="small" class="action-bar">
        <a-space size="large">
          <a-tag v-if="selectedCount" color="green" style="font-size: 13px">
            已挑选候选：{{ selectedSummary }}
          </a-tag>
          <a-button type="primary" size="large" :loading="deciding" @click="approve">
            ✓ 通过，生成分工与采购清单
          </a-button>
          <a-button danger size="large" :loading="deciding" @click="rejectModalOpen = true">
            ✗ 否决，带意见重新生成
          </a-button>
        </a-space>
      </a-card>
    </a-affix>

    <a-modal
      v-model:open="rejectModalOpen"
      title="否决并给出修改意见"
      :mask-closable="false"
      :confirm-loading="deciding"
      @ok="reject"
    >
      <a-alert
        type="info"
        show-icon
        message="意见会注入四路提案 prompt 重新生成，越具体越好"
        style="margin-bottom: 12px"
      />
      <a-textarea
        v-model:value="feedback"
        :rows="4"
        placeholder="例：餐饮不要火锅换素食自助；流程强度别太大，照顾体能弱的同事；参考备选2改室内"
      />
    </a-modal>
  </div>

  <a-result
    v-else
    status="404"
    title="没有待审批的方案"
    sub-title="请从首页发起新的策划流程"
  >
    <template #extra>
      <a-button type="primary" @click="$router.push('/')">回首页</a-button>
    </template>
  </a-result>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { useEventFlowStore } from '@/stores/eventFlow'
import ProposalCard from '@/components/ProposalCard.vue'
import BudgetSummary from '@/components/BudgetSummary.vue'
import {
  CATEGORY_LABEL,
  CATEGORY_ORDER,
  type BlockingIssue,
  type ProposalCategory,
  type RiskAlternative,
  type RiskFlagModel,
  type RuleStatusModel,
  type Selections,
} from '@/types'

const store = useEventFlowStore()
const router = useRouter()

const payload = computed(() => store.currentPayload)
const deciding = ref(false)
const rejectModalOpen = ref(false)
const feedback = ref('')

/** 多候选挑选结果: 交通等 = "候选名"; 餐饮逐顿 = { "D1|午餐": "候选餐厅名" }（随 approve/reject 提交后端） */
const selections = reactive<Selections>({})
const isSelected = (v: string | Record<string, string> | undefined): boolean =>
  typeof v === 'string' ? !!v : !!v && Object.keys(v).length > 0
const selectedCount = computed(() =>
  Object.values(selections).filter(isSelected).length,
)
const selectedSummary = computed(() =>
  Object.entries(selections)
    .filter(([, v]) => isSelected(v))
    .map(([k, v]) => {
      const label = CATEGORY_LABEL[k as ProposalCategory] ?? k
      if (typeof v === 'string') return `${label}: ${v}`
      const perMeal = Object.entries(v)
        .map(([meal, name]) => `${meal.replace('|', '·')}=${name}`)
        .join('、')
      return `${label}: ${perMeal}`
    })
    .join('，'),
)
// 新一轮 payload 到达时清空挑选(旧轮选择不残留)
watch(
  () => payload.value,
  (np, op) => {
    if (np !== op) {
      for (const k of Object.keys(selections)) delete selections[k]
    }
  },
)

const categoriesWithProposal = computed<ProposalCategory[]>(() =>
  CATEGORY_ORDER.filter((c) => payload.value?.proposals?.[c]),
)
const alternatives = computed<RiskAlternative[]>(() => payload.value?.risk_report?.alternatives ?? [])
const flags = computed<RiskFlagModel[]>(() => payload.value?.risk_report?.flags ?? [])
const ruleRows = computed<RuleStatusModel[]>(() => payload.value?.risk_report?.rules ?? [])
const uncovered = computed(() => payload.value?.risk_report?.uncovered_flags ?? [])
const unevaluated = computed(() => payload.value?.risk_report?.unevaluated_inputs ?? [])
// 阻断项与风险等级分开展示：等级答"方案本身多大风险"，它答"还有哪些事没闭合"
const blocking = computed<BlockingIssue[]>(() => payload.value?.blocking ?? [])

const historyProfile = computed(() => payload.value?.team_history?.profile ?? null)
const historyRetros = computed(() => payload.value?.team_history?.retros ?? [])
const historyVotes = computed(() => {
  const votes = historyProfile.value?.activity_votes
  if (!votes) return ''
  return Object.entries(votes)
    .map(([k, v]) => `${k}${v}票`)
    .join('、')
})

const RISK_TEXT: Record<string, string> = {
  high: '高风险',
  medium: '中风险',
  low: '低风险',
  unknown: '未评估',
}
const RISK_COLOR: Record<string, string> = {
  high: 'red',
  medium: 'orange',
  low: 'green',
  // 灰色而不是绿色：unknown 是"没查清"，不是"查了没事"。刷成绿色就等于替用户
  // 做了"可以放行"的判断
  unknown: 'default',
}
const riskColor = computed(() => RISK_COLOR[payload.value?.risk_report?.level ?? 'unknown'] ?? 'default')
const riskLevelText = computed(() => RISK_TEXT[payload.value?.risk_report?.level ?? 'unknown'] ?? '未评估')

const RULE_TEXT: Record<RuleStatusModel['status'], string> = {
  hit: '命中',
  clear: '已查无',
  not_evaluated: '未评估',
}
const RULE_COLOR: Record<RuleStatusModel['status'], string> = {
  hit: 'red',
  clear: 'green',
  not_evaluated: 'default',
}
function ruleText(s: RuleStatusModel['status']): string {
  return RULE_TEXT[s] ?? s
}
function ruleColor(s: RuleStatusModel['status']): string {
  return RULE_COLOR[s] ?? 'default'
}

function fmt(v: number): string {
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

/** 计划与预算的差：0 是"正好打平"，不是"没数据"，必须说出来 */
function deviationText(dev: number): string {
  if (dev > 0) return `计划超预算 ¥${fmt(dev)}`
  if (dev < 0) return `计划结余 ¥${fmt(-dev)}`
  return '计划与预算正好打平'
}

function citeAlternative(i: number, alt: RiskAlternative) {
  feedback.value = `${feedback.value}参考备选${i + 1}（${alt.plan}）。`.trim()
  rejectModalOpen.value = true
}

async function approve() {
  deciding.value = true
  try {
    await store.decide(true, undefined, cleanSelections())
    if (store.status === 'completed') {
      message.success('审批通过，分工与采购清单已生成')
      router.push('/plan')
    }
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    deciding.value = false
  }
}

async function reject() {
  if (!feedback.value.trim()) {
    message.warning('否决时必须填写修改意见')
    return
  }
  deciding.value = true
  try {
    await store.decide(false, feedback.value.trim(), cleanSelections())
    feedback.value = ''
    rejectModalOpen.value = false
    router.push('/generating') // 重新生成中（无 pendingLaunch 则只等 /resume 结果，不重新发起）
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    deciding.value = false
  }
}

/** 只提交用户实际勾选的候选(空值/空对象不传, 后端语义为"未挑选") */
function cleanSelections(): Selections {
  const out: Selections = {}
  for (const [k, v] of Object.entries(selections)) {
    if (isSelected(v)) out[k] = v
  }
  return out
}
</script>

<style scoped>
.action-bar {
  box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.08);
  text-align: center;
}
</style>
