<template>
  <a-card size="small" title="预算总览" class="budget-card">
    <div class="budget-head">
      <a-statistic title="四路合计" :value="budget.total" :precision="0" prefix="¥" />
      <a-statistic
        v-if="budget.budget_limit != null"
        title="预算上限"
        :value="budget.budget_limit"
        :precision="0"
        prefix="¥"
      />
      <!-- 国内习惯：超支=红, 结余=绿；over_budget 负数=结余（与后端约定一致） -->
      <a-tag v-if="budget.over_budget != null" :color="overBudget ? 'red' : 'green'" class="over-tag">
        {{ overBudget ? `超支 ¥${fmt(Math.abs(budget.over_budget))}` : `结余 ¥${fmt(Math.abs(budget.over_budget))}` }}
      </a-tag>
    </div>
    <a-progress
      v-if="budget.budget_limit"
      :percent="percent"
      :status="overBudget ? 'exception' : 'success'"
      :show-info="false"
    />
    <div class="by-category">
      <span v-for="cat in CATEGORY_ORDER" :key="cat" class="cat-item">
        {{ CATEGORY_LABEL[cat] }} ¥{{ fmt(budget.by_category[cat] ?? 0) }}
      </span>
    </div>
  </a-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { BudgetDetail } from '@/types'
import { CATEGORY_LABEL, CATEGORY_ORDER } from '@/types'

const props = defineProps<{ budget: BudgetDetail }>()

const overBudget = computed(() => (props.budget.over_budget ?? 0) > 0)
const percent = computed(() => {
  if (!props.budget.budget_limit) return 0
  return Math.min(100, Math.round((props.budget.total / props.budget.budget_limit) * 100))
})

function fmt(v: number): string {
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}
</script>

<style scoped>
.budget-head {
  display: flex;
  gap: 32px;
  align-items: center;
  margin-bottom: 8px;
}
.over-tag {
  font-size: 14px;
  padding: 4px 10px;
}
.by-category {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  color: #666;
  font-size: 13px;
}
</style>
