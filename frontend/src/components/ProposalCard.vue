<template>
  <a-card :title="title" size="small" class="proposal-card">
    <template #extra>
      <span class="total">¥{{ formatMoney(proposal.total_cost) }}</span>
    </template>
    <p v-if="proposal.description" class="desc">{{ proposal.description }}</p>

    <!-- 多候选挑选：交通/餐饮等提案给出 options，用户可切换(选中值经审批提交后端) -->
    <div v-if="options.length" class="options-block">
      <div class="options-title">候选方案（可挑选）：</div>
      <a-radio-group
        :value="modelValue"
        style="display: block"
        @update:value="(v: string) => emit('update:modelValue', v)"
      >
        <div
          v-for="opt in options"
          :key="opt.name"
          class="option-row"
          :class="{ active: modelValue === opt.name }"
          @click="emit('update:modelValue', opt.name)"
        >
          <a-radio :value="opt.name">
            <b>{{ opt.name }}</b>
            <span class="opt-cost">¥{{ formatMoney(opt.est_cost) }}</span>
          </a-radio>
          <div v-if="opt.reason" class="opt-reason">💡 {{ opt.reason }}</div>
          <div v-if="opt.note" class="opt-note">{{ opt.note }}</div>
        </div>
      </a-radio-group>
    </div>

    <!-- 餐饮逐顿候选: 每顿独立单选, 选择结果随审批提交(键 "D1|午餐") -->
    <div v-for="meal in meals" :key="mealKey(meal)" class="options-block">
      <div class="options-title">{{ meal.day }} · {{ meal.meal_type }}（候选餐厅，可挑选）：</div>
      <a-radio-group
        :value="mealSelection[mealKey(meal)]"
        style="display: block"
        @update:value="(v: string) => emitMeal(mealKey(meal), v)"
      >
        <div
          v-for="opt in meal.options"
          :key="opt.name"
          class="option-row"
          :class="{ active: mealSelection[mealKey(meal)] === opt.name }"
          @click="emitMeal(mealKey(meal), opt.name)"
        >
          <a-radio :value="opt.name">
            <b>{{ opt.name }}</b>
            <span class="opt-cost">¥{{ formatMoney(opt.est_cost) }}</span>
          </a-radio>
          <div v-if="opt.reason" class="opt-reason">💡 {{ opt.reason }}</div>
          <div v-if="opt.note" class="opt-note">{{ opt.note }}</div>
        </div>
      </a-radio-group>
    </div>

    <!-- 逐段转场路线(流程提案, 程序化调高德 direction 生成) -->
    <div v-if="routeSegments.length" class="options-block">
      <div class="options-title">转场路线（高德实时路线规划）：</div>
      <div v-for="(seg, i) in routeSegments" :key="i" class="seg-row">🚗 {{ seg }}</div>
    </div>

    <a-table
      :data-source="items"
      :columns="columns"
      :pagination="false"
      size="small"
      row-key="name"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'name'">
          {{ record.name }}
          <div v-if="record.location" class="loc">📍 {{ record.location.name }}</div>
          <div v-if="record.reason" class="loc">💡 {{ record.reason }}</div>
        </template>
      </template>
    </a-table>
  </a-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { MealOptions, Proposal, ProposalCategory } from '@/types'
import { CATEGORY_LABEL } from '@/types'

const props = defineProps<{
  proposal: Proposal
  category: ProposalCategory
  /** 当前选中的候选(v-model):
   *  单选类(交通等) = 候选名字符串; 餐饮逐顿 = { "D1|午餐": 候选餐厅名 } */
  modelValue?: string | Record<string, string>
}>()

const emit = defineEmits<{ (e: 'update:modelValue', v: string | Record<string, string>): void }>()

const title = computed(() => CATEGORY_LABEL[props.category] ?? props.category)
const items = computed(() => props.proposal.items ?? [])
const options = computed(() => props.proposal.options ?? [])
const meals = computed<MealOptions[]>(() => props.proposal.meals ?? [])
const routeSegments = computed<string[]>(() => props.proposal.route_segments ?? [])

/** 逐顿选择状态(modelValue 为对象时生效) */
const mealSelection = computed<Record<string, string>>(() =>
  typeof props.modelValue === 'object' && props.modelValue ? props.modelValue : {},
)

function mealKey(meal: MealOptions): string {
  return `${meal.day}|${meal.meal_type}`
}

function emitMeal(key: string, v: string) {
  emit('update:modelValue', { ...mealSelection.value, [key]: v })
}

const columns = [
  { title: '明细', dataIndex: 'name', key: 'name' },
  {
    title: '费用(元)',
    dataIndex: 'cost',
    key: 'cost',
    width: 110,
    customRender: ({ text }: { text: number }) => `¥${formatMoney(text)}`,
  },
  { title: '备注', dataIndex: 'note', key: 'note', ellipsis: true },
]

function formatMoney(v: number | null | undefined): string {
  return (v ?? 0).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}
</script>

<style scoped>
.proposal-card :deep(.ant-card-head-title) {
  font-weight: 600;
}
.total {
  color: #0f3d2e;
  font-weight: 700;
  font-size: 16px;
}
.desc {
  color: #555;
  margin-bottom: 12px;
  white-space: pre-wrap;
}
.options-block {
  margin-bottom: 12px;
}
.options-title {
  font-weight: 600;
  margin-bottom: 6px;
}
.option-row {
  padding: 6px 10px;
  border: 1px solid #eee;
  border-radius: 6px;
  margin-bottom: 6px;
  cursor: pointer;
  transition: background 0.2s;
}
.option-row:hover {
  background: #f6f8f7;
}
.option-row.active {
  border-color: #0f3d2e;
  background: #f0f7f3;
}
.opt-cost {
  color: #0f3d2e;
  font-weight: 600;
  margin-left: 8px;
}
.opt-note {
  color: #777;
  font-size: 12px;
  padding-left: 24px;
  white-space: pre-wrap;
}
.opt-reason {
  color: #0f3d2e;
  font-size: 12px;
  padding-left: 24px;
  margin-top: 2px;
  white-space: pre-wrap;
}
.loc {
  color: #0f3d2e;
  font-size: 12px;
  margin-top: 2px;
}
.seg-row {
  color: #444;
  font-size: 13px;
  padding: 3px 0;
  border-bottom: 1px dashed #eee;
}
</style>
