<template>
  <a-card size="small" title="执行清单（Checklist）">
    <!-- 清单是程序兜底生成的：说明白它为什么长这样，别让人以为模型就写成这样 -->
    <a-alert
      v-if="checklist.degraded"
      type="warning"
      show-icon
      message="这份清单由程序按已批准方案直接生成"
      :description="degradedReason"
      style="margin-bottom: 12px"
    />
    <!-- 程序校验发现的清单问题（超该路报价/归属认不出/清单为空） -->
    <a-alert
      v-if="warnings.length"
      type="warning"
      show-icon
      message="清单校验提示"
      style="margin-bottom: 12px"
    >
      <template #description>
        <div v-for="(w, i) in warnings" :key="i">· {{ w }}</div>
      </template>
    </a-alert>

    <a-tabs>
      <a-tab-pane key="assignments" :tab="`分工表 (${checklist.assignments.length})`">
        <a-table
          :data-source="assignmentRows"
          :columns="assignColumns"
          :pagination="false"
          size="small"
          row-key="_key"
        />
      </a-tab-pane>
      <a-tab-pane key="purchase" :tab="`采购清单 (${checklist.purchase_list.length})`">
        <template #tab>
          采购清单 ({{ checkedCount }}/{{ checklist.purchase_list.length }})
        </template>
        <div class="purchase-summary">
          已选合计：<b>¥{{ fmt(checkedCost) }}</b> / 全部约 ¥{{ fmt(totalCost) }}
          <span class="estimate-note">· 费用均为模型估算，未询价、未支付</span>
        </div>
        <!-- 勾选按**项的身份**记，不按第几项：清单重新生成后顺序会变，
             按下标记会让勾选静默跑到别的项上 -->
        <div v-for="row in purchaseRows" :key="row.key" class="purchase-row">
          <a-checkbox v-model:checked="checked[row.key]">
            <a-tag v-if="row.categoryLabel" color="blue">{{ row.categoryLabel }}</a-tag>
            <b>{{ row.item.item }}</b>
            <span v-if="row.item.quantity"> ×{{ row.item.quantity }}</span>
            <span class="cost">
              ¥{{ fmt(row.item.est_cost ?? 0) }}
              <span v-if="row.item.est_cost_is_estimate !== false" class="estimate-tag">估算</span>
            </span>
            <span v-if="row.item.note" class="note">{{ row.item.note }}</span>
          </a-checkbox>
        </div>
      </a-tab-pane>
      <a-tab-pane key="notice" tab="通知文案">
        <a-typography-paragraph class="notice-text">{{ checklist.notice_text }}</a-typography-paragraph>
        <a-button type="primary" @click="copyNotice">一键复制（发群）</a-button>
      </a-tab-pane>
    </a-tabs>
  </a-card>
</template>

<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { message } from 'ant-design-vue'
import { CATEGORY_LABEL, type PurchaseItem, type TaskChecklist } from '@/types'

const props = defineProps<{ checklist: TaskChecklist }>()

/** 采购项的勾选状态：键 = 项的身份（类别+名称+数量），不是数组下标 */
type CheckState = Record<string, boolean>

function itemKey(p: PurchaseItem): string {
  return `${p.category ?? '-'}|${p.item}|${p.quantity ?? 1}`
}

const checked = reactive<CheckState>(
  Object.fromEntries(props.checklist.purchase_list.map((p) => [itemKey(p), false])),
)
// 清单换了（重新生成/重来一轮）：重建勾选状态，只保留仍然存在的项
// —— 留着已经不在清单里的键，下次同名项会被莫名其妙地勾上
watch(
  () => props.checklist,
  (next) => {
    const keys = new Set(next.purchase_list.map(itemKey))
    for (const k of Object.keys(checked)) {
      if (!keys.has(k)) delete checked[k]
    }
    for (const k of keys) {
      if (!(k in checked)) checked[k] = false
    }
  },
)

/** 分工行：表行标识必须唯一，重名角色（两个"物资采购"）不能共用一行 */
const assignmentRows = computed(() =>
  props.checklist.assignments.map((a, i) => ({ ...a, _key: `${i}|${a.role}` })),
)

const purchaseRows = computed(() =>
  props.checklist.purchase_list.map((item) => ({
    key: itemKey(item),
    item,
    categoryLabel: item.category ? CATEGORY_LABEL[item.category] : '',
  })),
)

const warnings = computed(() => props.checklist.warnings ?? [])
const degradedReason = computed(() => {
  const first = warnings.value[0] ?? ''
  return first || '分工为通用模板、通知文案由程序拼装，建议人工过一遍。'
})

const assignColumns = [
  { title: '角色', dataIndex: 'role', key: 'role', width: 140 },
  { title: '负责人', dataIndex: 'owner', key: 'owner', width: 120 },
  { title: '说明', dataIndex: 'detail', key: 'detail' },
]

const checkedCount = computed(
  () => props.checklist.purchase_list.filter((p) => checked[itemKey(p)]).length,
)
const totalCost = computed(() =>
  props.checklist.purchase_list.reduce((s, p) => s + (p.est_cost ?? 0), 0),
)
const checkedCost = computed(() =>
  props.checklist.purchase_list.reduce(
    (s, p) => s + (checked[itemKey(p)] ? (p.est_cost ?? 0) : 0),
    0,
  ),
)

function fmt(v: number): string {
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

async function copyNotice() {
  try {
    await navigator.clipboard.writeText(props.checklist.notice_text)
    message.success('通知文案已复制，可直接发群')
  } catch {
    message.warning('复制失败，请手动选择文本复制')
  }
}
</script>

<style scoped>
.purchase-summary {
  margin-bottom: 10px;
  color: #555;
}
.estimate-note {
  color: #999;
  font-size: 12px;
}
.purchase-row {
  padding: 4px 0;
  border-bottom: 1px dashed #eee;
}
.cost {
  color: #0f3d2e;
  margin-left: 8px;
}
.estimate-tag {
  color: #999;
  font-size: 12px;
  margin-left: 4px;
}
.note {
  color: #999;
  margin-left: 8px;
  font-size: 12px;
}
.notice-text {
  white-space: pre-wrap;
  background: #f6f8f7;
  padding: 12px;
  border-radius: 6px;
}
</style>