<template>
  <div>
    <a-alert
      v-if="store.hasRestorableSession"
      type="info"
      show-icon
      class="restore-alert"
      :message="`检测到未完成的审批（thread: ${shortThread}）`"
    >
      <template #action>
        <a-button size="small" type="primary" @click="$router.push('/approval')">
          恢复审批现场
        </a-button>
        <a-button size="small" style="margin-left: 8px" @click="store.reset()">放弃</a-button>
      </template>
    </a-alert>

    <!-- ===== 需求信息 ===== -->
    <a-card title="填写团建需求">
      <a-form layout="vertical">
        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item label="活动类型" required>
              <a-select v-model:value="form.activityType" :options="ACTIVITY_TYPES" />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="活动日期（从几号到几号）" required>
              <a-range-picker
                v-model:value="form.dateRange"
                style="width: 100%"
                value-format="YYYY-MM-DD"
                :placeholder="['开始日期', '结束日期']"
              />
            </a-form-item>
          </a-col>
        </a-row>

        <!-- 地点: 省/市/区县三级选择 + 一个"更具体的地点"
             选择器里的省市县是**权威归属**: 后端不再靠"高德热度首位"猜(跨省重名
             时会赌歪)。detail 则是**锚点**——填「老君山」就在老君山 35km 内找场地,
             不填就按所选城市的范围找。 -->
        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item label="出发地（省 / 市 / 区县）" required>
              <a-row :gutter="8">
                <a-col :span="14">
                  <a-cascader
                    v-model:value="form.originAdmin"
                    :options="areaOptions"
                    :loading="areaLoading"
                    :field-names="AREA_FIELD_NAMES"
                    change-on-select
                    allow-clear
                    show-search
                    :filter-option="filterAreaOption"
                    placeholder="如：上海市 / 浦东新区"
                  />
                </a-col>
                <a-col :span="10">
                  <a-input
                    v-model:value="form.originDetail"
                    placeholder="更具体地点（选填）"
                    :maxlength="50"
                  />
                </a-col>
              </a-row>
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="目的地（同城可留空）">
              <a-row :gutter="8">
                <a-col :span="14">
                  <a-cascader
                    v-model:value="form.destinationAdmin"
                    :options="areaOptions"
                    :loading="areaLoading"
                    :field-names="AREA_FIELD_NAMES"
                    change-on-select
                    allow-clear
                    show-search
                    :filter-option="filterAreaOption"
                    placeholder="如：河南省 / 洛阳市 / 栾川县"
                  />
                </a-col>
                <a-col :span="10">
                  <a-input
                    v-model:value="form.destinationDetail"
                    placeholder="更具体地点，如 老君山"
                    :maxlength="50"
                  />
                </a-col>
              </a-row>
            </a-form-item>
          </a-col>
        </a-row>

        <a-row :gutter="16">
          <a-col :span="8">
            <a-form-item label="参与人数" required>
              <a-input-number v-model:value="form.peopleCount" :min="1" :max="500" style="width: 100%" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="人均预算（元）">
              <a-input-number v-model:value="form.budgetPerPerson" :min="0" style="width: 100%" placeholder="不填=由方案估算" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item label="优先通行方式">
              <a-select v-model:value="form.transportPref" :options="TRANSPORT_PREFS" allow-clear />
            </a-form-item>
          </a-col>
        </a-row>

        <a-form-item label="团队 ID（自动生成，团队记忆按此隔离）">
          <div class="team-hint">
            <template v-if="store.teamId">
              本机团队 ID：{{ store.teamId }}（后续策划会自动带上，历史偏好自动复用）
            </template>
            <template v-else>
              首次策划时自动生成；团队历史（画像/复盘）按此隔离，无需也不支持手填
            </template>
          </div>
        </a-form-item>
      </a-form>
    </a-card>

    <!-- ===== 成员偏好（独立模块，与需求同页提交） ===== -->
    <a-card title="成员偏好" style="margin-top: 16px">
      <template #extra>
        <a-radio-group v-model:value="prefMode" button-style="solid" size="small">
          <a-radio-button value="aggregate">全员汇总</a-radio-button>
          <a-radio-button value="perPerson">逐人添加</a-radio-button>
        </a-radio-group>
      </template>

      <a-alert
        type="info"
        show-icon
        style="margin-bottom: 16px"
        :message="prefMode === 'aggregate'
          ? '一份偏好代表所有人（组织者收齐意见后统一填）'
          : '逐个添加成员偏好，系统按人投票/聚合'"
        description="每位成员至少填写「活动偏好」或「忌口」其中一项（体能可不填）。同名不同写会自动归一，例如 登山/徒步 → 爬山。"
      />

      <!-- 模式 A：全员汇总 -->
      <a-form v-if="prefMode === 'aggregate'" layout="vertical">
        <a-row :gutter="16">
          <a-col :span="12">
            <a-form-item label="活动偏好（多选，可自定义；活动与忌口至少填一项）">
              <a-select
                :value="aggregate.activityPrefs"
                mode="tags"
                :options="PREF_SUGGESTIONS"
                placeholder="选择或输入：爬山 / 剧本杀 / 聚餐 / 飞盘…"
                @change="(v: string[]) => (aggregate.activityPrefs = foldActivities(v))"
              />
            </a-form-item>
          </a-col>
          <a-col :span="12">
            <a-form-item label="忌口 / 饮食标签（多选，可自定义）">
              <a-select
                :value="aggregate.dietary"
                mode="tags"
                :options="DIETARY_SUGGESTIONS"
                placeholder="素食 / 清真 / 不吃辣 / 海鲜过敏…可直接输入"
                @change="(v: string[]) => (aggregate.dietary = foldDietary(v))"
              />
            </a-form-item>
          </a-col>
        </a-row>
        <a-form-item label="团队平均体能（不填则不计入体能分布）">
          <a-select
            v-model:value="aggregate.fitnessLevel"
            :options="FITNESS_OPTIONS"
            allow-clear
            placeholder="未填写（不参与体能统计）"
            style="max-width: 240px"
          />
        </a-form-item>
      </a-form>

      <!-- 模式 B：逐人添加 -->
      <div v-else>
        <a-empty v-if="!persons.length" description="还没有成员，点下方「添加成员」" />
        <a-card
          v-for="(p, i) in persons"
          :key="i"
          size="small"
          :title="`成员 ${i + 1}${p.person_name ? '·' + p.person_name : ''}`"
          style="margin-bottom: 12px"
        >
          <template #extra>
            <a style="color: #ff4d4f" @click="removePerson(i)">删除</a>
          </template>
          <a-form layout="vertical">
            <a-row :gutter="16">
              <a-col :span="6">
                <a-form-item label="姓名/标识" required>
                  <a-input v-model:value="p.person_name" placeholder="张三" />
                </a-form-item>
              </a-col>
              <a-col :span="9">
                <a-form-item label="活动偏好（多选，可自定义；与忌口至少填一项）">
                  <a-select
                    :value="p.activity_prefs"
                    mode="tags"
                    :options="PREF_SUGGESTIONS"
                    placeholder="可同时选爬山+聚餐，或直接输入"
                    @change="(v: string[]) => (p.activity_prefs = foldActivities(v))"
                  />
                </a-form-item>
              </a-col>
              <a-col :span="9">
                <a-form-item label="忌口（多选，可自定义）">
                  <a-select
                    :value="p.dietary"
                    mode="tags"
                    :options="DIETARY_SUGGESTIONS"
                    placeholder="素食 / 不吃辣…可直接输入"
                    @change="(v: string[]) => (p.dietary = foldDietary(v))"
                  />
                </a-form-item>
              </a-col>
            </a-row>
            <a-row :gutter="16">
              <a-col :span="12">
                <a-form-item label="体能（不填则不计入体能分布）">
                  <a-select
                    v-model:value="p.fitness_level"
                    :options="FITNESS_OPTIONS"
                    allow-clear
                    placeholder="未填写（不参与体能统计）"
                  />
                </a-form-item>
              </a-col>
            </a-row>
          </a-form>
        </a-card>
        <a-button type="dashed" block @click="addPerson">➕ 添加成员</a-button>
      </div>

      <a-divider />

      <a-alert style="margin-bottom: 12px" type="info" show-icon
               message="需求预览（仅展示，提交的是上面的结构化字段）" :description="composedRawText" />

      <a-button type="primary" size="large" :loading="launching" @click="launchNow">
        🚀 生成方案（含偏好，一步提交）
      </a-button>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { useEventFlowStore } from '@/stores/eventFlow'
import { DIETARY_SUGGESTIONS, PREF_SUGGESTIONS, foldActivities, foldDietary } from '@/utils/lexicon'
import type { PersonPref, PlaceInput, PlanStartBody } from '@/types'
import type { AreaNode } from '@/data/china-area'

const store = useEventFlowStore()
const router = useRouter()

// ---- 行政区划静态表(省/市/区县) ----
// 140KB, 所以**懒加载**: 它只给下拉框填选项, 不该拖慢首屏。表里带 adcode, 但
// adcode 不是权威(县级区划每年都有撤县设区, 表必然滞后)—— 提交时省市县发的是
// **名字**, 后端拿名字去高德解析归属, 表旧了最多是下拉里多一个已撤销的选项。
const areaOptions = ref<AreaNode[]>([])
const areaLoading = ref(false)
// 级联的节点是 {name, code, children}, 与 a-cascader 默认的 {label, value} 不同名
const AREA_FIELD_NAMES = { label: 'name', value: 'code', children: 'children' }

onMounted(async () => {
  areaLoading.value = true
  try {
    const mod = await import('@/data/china-area')
    areaOptions.value = mod.CHINA_AREA
  } catch (e) {
    // 加载不出来不阻塞提交: 用户可以只填"更具体的地点", 后端照样能解析
    message.warning('省市县列表加载失败，可直接在右侧填写具体地点')
    console.error('[Intake] 区划表加载失败', e)
  } finally {
    areaLoading.value = false
  }
})

/** 级联搜索: 逐级匹配名字(3000+ 个区县, 不搜索基本没法用) */
function filterAreaOption(input: string, path: AreaNode[]): boolean {
  const q = input.trim()
  if (!q) return true
  return path.some((node) => node.name.includes(q))
}

const ACTIVITY_TYPES = [
  { value: '团建', label: '团建' },
  { value: '户外拓展', label: '户外拓展' },
  { value: '生日会', label: '生日会' },
  { value: '聚餐', label: '聚餐' },
  { value: '剧本杀', label: '剧本杀' },
]
const TRANSPORT_PREFS = [
  { value: '自驾', label: '🚗 自驾拼车' },
  { value: '高铁', label: '🚄 高铁/火车' },
  { value: '大巴', label: '🚌 大巴包车' },
  { value: '打车', label: '🚕 打车/包车' },
  { value: '不限', label: '不限（由 AI 按人数/距离定）' },
]
// 活动/忌口建议项与同义归一从 @/utils/lexicon 引入(mirror backend/app/graph/lexicon.py):
// a-select mode="tags" 保留自由输入(冷门活动不能被模板吞掉), 但输入后即时归一,
// 免得"爬山/登山/徒步"各自成桶, 最后写出"10人想爬山 vs 8人想登山"这种假分歧
const FITNESS_OPTIONS = [
  { value: 1, label: '1 级 · 佛系' },
  { value: 2, label: '2 级 · 偏弱' },
  { value: 3, label: '3 级 · 正常' },
  { value: 4, label: '4 级 · 不错' },
  { value: 5, label: '5 级 · 达人' },
]

const launching = ref(false)

const form = reactive({
  activityType: '团建',
  // 地点: 级联选中的 code 路径(['41','4103','410324'], change-on-select 可以只到市)
  // + 一个可选的"更具体的地点"。两者都不再是自由文本 —— 这是这次改动的核心:
  // 归属由用户选出来, 后端不必再猜。
  originAdmin: [] as string[],
  originDetail: '',
  destinationAdmin: [] as string[],
  destinationDetail: '',
  dateRange: null as [string, string] | null,
  peopleCount: null as number | null,
  budgetPerPerson: null as number | null,
  transportPref: '不限',
})

/** 级联的 code 路径 -> 命中的 {province, city, district} 节点 */
function resolveArea(path: string[] | undefined) {
  const picked = path ?? []
  const province = areaOptions.value.find((p) => p.code === picked[0])
  const city = province?.children?.find((c) => c.code === picked[1])
  const district = city?.children?.find((d) => d.code === picked[2])
  return { province, city, district }
}

/** 提交给后端的扁平串**只装规范化城市名, 绝不带 detail**:
 *  ① 后端判"同城/跨城"是字符串相等, 带上 detail 会让同城的两次不同选择
 *     (…老君山 vs …龙门石窟)判成跨城, 市内行程被排上高铁;
 *  ② 出发地这个串会**原样**当火车出发站, 长串会让站点匹配失败 -> 火车恒为空;
 *  ③ 区县名太细: 天气/攻略都按城市查。 */
function flatCity(area: ReturnType<typeof resolveArea>): string {
  return area.city?.name || area.province?.name || ''
}

/** 级联选择 + detail -> PlaceInput(与后端 PlanStartBody.origin_place 同形) */
function buildPlaceInput(
  adminPath: string[],
  detail: string,
): PlaceInput | null {
  const { province, city, district } = resolveArea(adminPath)
  const text = detail.trim()
  if (!province && !text) return null
  return {
    province: province?.name ?? null,
    city: city?.name ?? null,
    district: district?.name ?? null,
    // adcode 只作交叉校验: 后端按名字解析归属, 对不上会写进日志, 不静默采信
    adcode: district?.code ?? city?.code ?? province?.code ?? null,
    detail: text || null,
  }
}

// ---- 偏好模块: 两种填写模式二选一 ----
const prefMode = ref<'aggregate' | 'perPerson'>('aggregate')
// 模式 A: 一份代表全员(体能/活动/忌口; 预算沿用上方"人均预算"字段)
// 体能默认 null = 未填写: 默认值 3 会让"没考虑过体能"的团队静默按 3 级进统计,
// 稀释弱体能占比(阈值 25%)从而掩盖真冲突; 不填就是不填
const aggregate = reactive({
  activityPrefs: [] as string[],
  dietary: [] as string[],
  fitnessLevel: null as number | null,
})
// 模式 B: 逐人添加, 每人一条完整偏好
const persons = ref<PersonPref[]>([])

function addPerson() {
  persons.value.push({
    person_name: '',
    headcount: 1,
    activity_prefs: [],
    fitness_level: null,
    dietary: [],
  })
}
function removePerson(i: number) {
  persons.value.splice(i, 1)
}

const shortThread = computed(() => store.threadId.slice(0, 8))

/** 日期区间描述: 单日 "10-18"; 多日 "10-18到10-20共3天" */
function dateRangeText(): string {
  const [ds, de] = form.dateRange ?? []
  if (!ds) return ''
  if (!de || de === ds) return ds
  const days = Math.round((new Date(de).getTime() - new Date(ds).getTime()) / 86400000) + 1
  return `${ds}到${de}共${days}天`
}

/** 取当前偏好模式下所有活动偏好(去重, 保持输入顺序) */
function collectActivityPrefs(): string[] {
  if (prefMode.value === 'aggregate') return [...aggregate.activityPrefs]
  const seen = new Set<string>()
  for (const p of persons.value) for (const a of p.activity_prefs ?? []) seen.add(a)
  return [...seen]
}
/** 取当前偏好模式下所有忌口(去重) */
function collectDietary(): string[] {
  if (prefMode.value === 'aggregate') return [...aggregate.dietary]
  const seen = new Set<string>()
  for (const p of persons.value) for (const d of p.dietary ?? []) seen.add(d)
  return [...seen]
}

/** 级联选择 + detail -> 给人看的完整地名(如"河南省洛阳市栾川县老君山") */
function placeText(adminPath: string[], detail: string): string {
  const { province, city, district } = resolveArea(adminPath)
  return [province?.name, city?.name, district?.name, detail.trim()]
    .filter(Boolean)
    .join('')
}

/** 表单 -> 需求预览文本(仅用于页面展示与留痕 raw_text)
 *  注意: 这**不是**提交给后端的抽取输入 —— /plan 收的是下面的结构化字段,
 *  后端 parse_request 走零 LLM 分支直接组装, 不再从这句话里猜字段 */
const composedRawText = computed(() => {
  const parts: string[] = []
  parts.push(`${form.peopleCount ?? '?'}人${form.activityType}`)
  const dr = dateRangeText()
  if (dr) parts.push(dr)
  // 预览用全路径 + detail(读着像人话); 提交的 origin/destination 是**城市名**
  const o = placeText(form.originAdmin, form.originDetail) || '?'
  const d = placeText(form.destinationAdmin, form.destinationDetail)
  parts.push(d ? `从${o}出发到${d}` : `${o}周边/同城`)
  if (form.budgetPerPerson) parts.push(`人均预算${form.budgetPerPerson}元(总${(form.budgetPerPerson * (form.peopleCount || 0)).toFixed(0)}元)`)
  if (form.transportPref && form.transportPref !== '不限') parts.push(`优先${form.transportPref}出行`)
  const acts = collectActivityPrefs()
  const diets = collectDietary()
  if (acts.length) parts.push(`偏好${acts.join('/')}`)
  if (diets.length) parts.push(`忌口${diets.join('/')}`)
  return parts.join('，')
})

/** 偏好模块 -> PersonPref 列表
 *  计数口径由每条的 headcount **声明**, 不再靠后端猜"是不是汇总行":
 *  全员汇总 -> 单条, headcount = 总人数; 逐人 -> N 条, headcount = 1。
 *  headcount 之和不得超过总人数(后端会 422), 差额是"未表态", 允许。
 *  "至少要填一项"判断的是活动/忌口, 不看体能 —— 体能是可选补充信息,
 *  不能因为没填体能就把这个人(和 TA 的忌口约束)整条丢掉。 */
function buildPreferences(): PersonPref[] {
  if (prefMode.value === 'aggregate') {
    if (!aggregate.activityPrefs.length && !aggregate.dietary.length) return []
    return [{
      person_name: '全员(表单汇总)',
      headcount: form.peopleCount ?? 1,
      activity_prefs: [...aggregate.activityPrefs],
      fitness_level: aggregate.fitnessLevel,
      dietary: [...aggregate.dietary],
    }]
  }
  return persons.value
    .filter((p) => p.person_name.trim() && ((p.activity_prefs ?? []).length || (p.dietary ?? []).length))
    .map((p) => ({
      person_name: p.person_name.trim(),
      headcount: 1,
      activity_prefs: [...(p.activity_prefs ?? [])],
      fitness_level: p.fitness_level,
      dietary: [...(p.dietary ?? [])],
    }))
}

function validate(): boolean {
  // 至少选到**市**: 只选到省的话后端无从判断归属, 火车/天气也定位不到。
  // (省级直辖的县级市本身就在第二级, 所以"≥2 级"对它们同样成立)
  if (form.originAdmin.length < 2) { message.warning('请选择出发地（至少选到市）'); return false }
  if (!form.dateRange?.[0]) { message.warning('请选择活动日期（从几号到几号，单日选同一天）'); return false }
  if (!form.peopleCount) { message.warning('请填写参与人数'); return false }
  // "至少要填一项"只看活动/忌口, 不看体能: 只填忌口的成员(素食、对玩什么没意见)
  // 必须能表达出来, 否则 dietary_all 会少数人, 餐饮方案就漏掉这条约束。
  // 体能是可选补充信息, 不能因为没填就把整条偏好丢掉。
  if (prefMode.value === 'aggregate') {
    if (!aggregate.activityPrefs.length && !aggregate.dietary.length) {
      message.warning('请至少填写一项活动偏好或忌口'); return false
    }
  } else {
    const named = persons.value.filter((p) => p.person_name.trim())
    if (!named.length) { message.warning('请至少添加一位成员，并填写姓名'); return false }
    if (!named.some((p) => (p.activity_prefs ?? []).length || (p.dietary ?? []).length)) {
      message.warning('请至少为一位成员填写活动偏好或忌口'); return false
    }
    const empty = named.find((p) => !(p.activity_prefs ?? []).length && !(p.dietary ?? []).length)
    if (empty) {
      message.warning(`成员「${empty.person_name}」的活动偏好和忌口都是空的（不需要请删除该成员）`); return false
    }
    // headcount 之和不得超过总人数: 与后端 PlanStartBody 的校验一致, 提前拦省一趟 422
    const declared = named.length
    if (declared > form.peopleCount) {
      message.warning(`成员数（${declared}）超过参与人数（${form.peopleCount}）`); return false
    }
  }
  return true
}

/** 表单 -> /plan 结构化请求体
 *  预算口径: 表单填的是"人均预算", 而后端 budget 一律按**总预算**参与校验
 *  (reconcile 拿四路报价合计和它比)。所以在这里乘人数换算成总预算 ——
 *  旧版把这个换算交给 LLM 从"人均预算1000元(总30000元)"里猜, 抽错就永远判超支。 */
function buildPlanPayload(): PlanStartBody {
  const [ds, de] = form.dateRange ?? []
  const count = form.peopleCount ?? 0
  const prefs = buildPreferences()
  const originArea = resolveArea(form.originAdmin)
  const destinationArea = resolveArea(form.destinationAdmin)
  return {
    people_count: count,
    date: ds ?? '',
    date_end: de && de !== ds ? de : null,
    budget: form.budgetPerPerson ? form.budgetPerPerson * count : null,
    // 扁平串 = 规范化城市名(见 flatCity 注释: 后端判同城是字符串相等、出发地
    // 会原样当火车出发站)
    origin: flatCity(originArea),
    destination: flatCity(destinationArea) || null,
    // 结构化地点入参: 省市县是**权威归属**, detail 只当锚点坐标与检索词。
    // 它们只进 raw_request["structured"], 不进 EventRequest —— 后者同时是自由
    // 文本路径的 LLM 工具 schema, 挂上去会让 LLM 幻觉出一份"权威归属"
    origin_place: buildPlaceInput(form.originAdmin, form.originDetail),
    destination_place: buildPlaceInput(form.destinationAdmin, form.destinationDetail),
    transport_pref: form.transportPref === '不限' ? null : form.transportPref,
    activity_type: form.activityType,
    raw_text: composedRawText.value.slice(0, 500),
    preferences: prefs.length ? prefs : undefined,
  }
}

async function launchNow() {
  if (!validate()) return
  launching.value = true
  store.pendingLaunch = { payload: buildPlanPayload() }
  store.error = ''
  router.push('/generating')
}
</script>

<style scoped>
.restore-alert {
  margin-bottom: 16px;
}
.team-hint {
  margin-top: 6px;
  color: #888;
  font-size: 12px;
}
</style>
