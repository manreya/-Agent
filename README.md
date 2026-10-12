# 团建活动策划 Agent 🎯

基于 **FastAPI + LangGraph** 的多人团建活动策划 Agent：输入一句需求，自动完成 需求解析 → 偏好聚合 → 场地/天气/路线采集 → 交通/场地/流程/餐饮四路并行报价 → 预算时间线约束消解 → 风险评估 → **人工审批（HITL）** → 任务清单与团队记忆沉淀。全程持久化，支持进程重启后从审批闸门恢复。

> 前身是单人旅行规划助手，已整体改造为多人团建场景；旅行规划相关代码与接口均已下线（见文末「前端适配清单」）。

## ✨ 核心能力

- **需求解析**：结构化输入（表单/API）直接组装，**零 LLM**；仅自由文本走一次 LLM 抽取，缺关键字段程序化返回 `missing_fields`
- **偏好聚合**：偏好随策划请求一次提交，**全程零 LLM**（纯 Python 统计 + 显式阈值规则生成提示，同一输入逐字同输出）。计数口径由每行 `headcount` **声明**（全员汇总行=总人数、逐人行=1），不再靠行数猜人数；活动/忌口先做同义归一（爬山/登山/徒步 合为一个桶），避免"10人想爬山 vs 8人想登山"这类假分歧
- **基础信息采集**：高德 REST 直连（POI 搜索/天气预报/地理编码），三路并行 + 进程级缓存
- **四路并行提案**：LangGraph Send API 并行派发 transport/venue/flow/food 四路 LLM 报价，餐饮路带受控搜索工具（最多 3 轮）
- **约束消解（reconcile）**：预算/时间线/容量三检纯程序零 LLM；未通过带具体数字回炉重生成（最多 2 次），超限交人工裁决。每条冲突都带**归属**（哪一路该修它，预算这类全局冲突不归任何一路）：回炉只重派相关的那几路，每路的 prompt 也只看到与自己相关或全局的冲突；回炉按 key 后写覆盖前写，所以本轮没给出明细时**保留上一轮**而不是换成一个 0 元空壳
- **风险评估（risk）**：规则层（雨天+室外项目 / 活动强度 vs 体能结构）纯 Python；有标记才调一次 LLM 生成带报价的室内/分层备选
- **人工审批（HITL）**：`interrupt()` 暂停于审批闸门，方案/预算明细/偏好提示（分歧 / 忌口约束）/风险备选推给前端；通过走收尾，否决带意见重走
- **任务清单（checklist）**：一次 LLM 把最终方案拆成 分工表/采购清单（带预算）/可直接发群的通知文案
- **团队记忆（memory）**：按 `team_id` 隔离写入 团队画像 + 复盘标签；下次策划自动注入"上次你们……"上下文。`team_id` 只认服务端签发的 `team_<32位hex>` 形态，手填的会被忽略并重新生成（明文可猜的 id 不是隔离，更不是鉴权）

## 输入层（第一步的边界与口径）

用户输入这一层把口径钉死，避免"缺字段照样跑完整流程"和"结构化数据绕一圈让 LLM 再猜一遍"：

| 约束 | 说明 |
|---|---|
| 结构化优先 | `/plan` 直接收结构化字段；`parse_request` 结构化分支**零 LLM 调用**（表单已有的精确值不再让模型重抽）。只有 `/parse` 的自由文本才走 LLM 抽取 |
| 必填 | 人数 / 开始日期 / 出发地（后端 schema 强制 → 缺失**422**，不消耗 LLM / 外部额度） |
| 偏好必填 | 每位成员「活动偏好」或「忌口」**至少填一项**（体能不算，它只是可选补充信息）。只填忌口的成员必须能表达出来，否则餐饮方案会漏掉这条约束 |
| 偏好计数口径 | 每行 `headcount` **声明**代表几人（全员汇总行=总人数，逐人行=1）——不再用 `len(prefs) == 1` 猜。`sum(headcount) ≤ people_count`（超出 422），差额=**未表态**（允许，不强制相等），摘要里 `stated_people` 如实给出票数基数 |
| 活动/忌口归一 | 统计前折叠同义写法（爬山/登山/徒步 → 爬山；吃素/纯素 → 素食），只覆盖高频词，未命中的标签**原样保留**（自由输入的口子不关，冷门活动不会被吞） |
| 偏好提示 | **确定性规则生成**（非 LLM，可重复、无幻觉）：活动分歧要求次高票 ≥ 最高票 50% **且**两者之和 ≤ 已表态人数（"汇总行里全员都勾了两样"不构成对立）；体能分层看已表态者中弱/强各占 25%；忌口措辞为**约束**而非分歧 |
| 已下线 | `budget_median` 与每人"预算意愿"：口径随填写模式漂移、逐人模式下恒为 0 且无人消费，按"无消费方不保留"删除 |
| 预算口径 | **一律总预算**（表单"人均预算"在前端 × 人数换算后提交）；下游 reconcile 拿四路报价合计与之比对 |
| 日期口径 | **YYYY-MM-DD**；`2026/10/01`、`2026年10月1日` 等写法归一化，缺年份视为抽取失败（不替用户猜年份），早于今天一律拒绝 |
| 长度上限 | `raw_text` ≤ 500 字、偏好 ≤ 200 条、`person_name` ≤ 50 字 |
| 日期基准 | 解析 prompt 注入**服务器当天日期**（相对表述如"10月底"才有推断依据；日期由服务端提供，不来自用户输入） |

## 🏗️ 架构

```mermaid
graph TD
    START([START]) --> PARSE[parse_request<br/>结构化=直接组装(零LLM)<br/>自由文本=LLM抽取+团队历史]
    PARSE --> AGG[aggregate_prefs<br/>纯Python统计 + 规则生成提示<br/>全程零LLM]
    AGG --> GATHER[gather_info<br/> venues/weather/geocode 三路并行<br/>汇合后查路线, 全程零LLM]
    GATHER --> DISPATCH{dispatch_proposals<br/>Send 派发}
    DISPATCH --> T[propose_transport]
    DISPATCH --> V[propose_venue]
    DISPATCH --> F[propose_flow]
    DISPATCH --> D[propose_food<br/>受控搜索工具≤3轮]
    T --> REC
    V --> REC
    F --> REC
    D --> REC[reconcile<br/>预算/时间线/容量 纯程序]
    REC -->|未过 且 replan≤2| DISPATCH
    REC -->|通过 / 超限| RISK[risk<br/>规则层零LLM<br/>有标记才调LLM备选]
    RISK --> APPROVAL[approval<br/>interrupt 人工审批]
    APPROVAL -->|approved=False<br/>意见注入四路 prompt| AGG
    APPROVAL -->|approved=True| CHECK[checklist<br/>分工/采购/通知文案 1次LLM]
    CHECK --> MEM[memory<br/>团队画像+复盘 写入 Store<br/>namespace=team_id]
    MEM --> DONE([END])
    MEM2 -.TODO 占位.- > PAY[第二个 interrupt<br/>付款/通知等不可逆动作]
```

- 状态持久化：`AsyncSqliteSaver` → `backend/data/checkpoints.db`（HITL 跨进程恢复）
- 团队记忆：`AsyncSqliteStore` → `backend/data/memory.db`（team_id 隔离）
- 结构化输出统一走 `bind_tools + tool_choice='required'` 封装（`app/graph/model.py`，DeepSeek 已调通）

## 🎬 核心演示（录屏占位）

| 演示 | 说明 | 录屏 |
|---|---|---|
| 四路并行提案 + 回炉 | Send 派发四路 LLM 时间戳重叠；reconcile 检出超支带数字回炉 | <!-- TODO: 补录屏 --> `docs/screenshots/demo-parallel-send.mp4`（占位） |
| 杀进程恢复 | 发起 → 暂停审批 → 杀掉服务进程 → 重启 → 同 thread_id resume，四路报价逐位一致 | <!-- TODO: 补录屏 --> `docs/screenshots/demo-kill-restart.mp4`（占位） |

## 🚀 快速开始

### 后端

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env    # 复制模板后填入自己的 Key
# 至少需配置: LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID / AMAP_API_KEY
uvicorn app.api.main:app --port 8000
# Swagger: http://localhost:8000/docs
```

> 其他数据源（博查搜索 / 聚合火车票 / Unsplash）未配置时会优雅降级，不阻塞主流程，详见 `backend/.env.example` 内的注释。

### 前端

```bash
cd frontend
npm install
cp .env.example .env    # 可选: 默认留空即走 vite 代理指向 localhost:8000
npm run dev   # http://localhost:5173
```

### 评测

```bash
cd backend
python -m evals.run_evals        # 全绿=通过; 失败输出明细, 退出码非0
# 用例集: backend/evals/golden_cases.json (5个金标准用例)
# 报告:   backend/evals/report.json (通过率 + 每用例断言明细)
# 断言: 预算不超(除非ConflictReport已上报) / 时间线不重叠(与 reconcile 共用
#       nodes.extract_time_ranges, 日期片段不会被误判为时段) / 场地名∈真实POI候选(防幻觉)
#       / LLM-as-judge 合理性评分>=3(便宜模型, env LLM_JUDGE_MODEL 可指定)
```

### 全量功能验证（开发用）

```bash
cd backend
python scripts/verify_model_phase.py   # 12 节验证: 模型/采集/并行/回环/风险/HITL/checklist/memory
```

## 📄 API 文档

启动后访问 `http://localhost:8000/docs`。核心端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/event/parse` | 需求解析（**自由文本入口**，唯一会调 LLM 抽取的端点）：raw_text → EventRequest；缺人数/日期/出发地或日期已过期返回 400+missing_fields |
| POST | `/api/event/plan` | 发起完整策划流程（**结构化入参**：人数/日期/出发地必填，预算=总预算），跑到审批闸门挂起；返回 `thread_id + approval_payload` |
| POST | `/api/event/resume` | 提交审批结果 `{thread_id, approved, feedback?}`；通过→最终方案+checklist，否决→带意见重走后再次暂停 |
| GET | `/api/event/health` | 健康检查（节点清单/持久化路径 + `checkpoint_writable`/`store_writable` 写权限探活；库不可写时返回 `degraded`） |
| GET | `/api/map/poi`、`/api/map/weather`、`POST /api/map/route` | 通用地图工具（高德 REST 直连，与策划主流程同一条链路：地名归一化 + 城市归属过滤） |
| GET | `/api/poi/detail/{id}`、`/api/poi/search`、`/api/poi/photo` | POI 详情/搜索/景点图片 |

**典型时序**：`/plan` 拿到 `status=pending_approval` 和 `approval_payload` → 审批页渲染 → 用户点通过/否决 → `/resume` 提交 → 通过后 `data` 里含四路提案 + `checklist`（分工/采购/通知文案，前端渲染可勾选清单）。

## 🛡️ 数据可信性防线（改动前请先读）

地名类数据最容易出错，本仓库在这几处做了程序化防护，**不要因为"看起来绕"就绕开**：

| 防线 | 位置 | 防的是什么 |
|---|---|---|
| 地名归一 `resolve_city` | `amap_client.py` | 高德 `place/text` 的 `city` 参数只认规范城市名，传「杭州西湖区」会被**静默忽略**（`citylimit` 失效）而返回全国结果——实测首条落在新疆伊犁（距杭州 3505km） |
| 目的地口径分流 `_kind_of` / `_search_scope` | `amap_client.py` | 「杭州」和「莫干山」是两种东西：前者该全市搜，后者该在它 35km 内搜。景区/古镇/离岛**不能**当城市查——实测 `geocode("莫干山")` 10 条全是甘肃/广西同名住宅区，`geocode("乌镇")` 全是陕西/西藏同名乡镇，故改由 `place/text` 热度首位反查行政归属；检索改用「{地名} {关键词}」且**不传 `city`/`citylimit`**（实测传了 city 反而返回 0 条） |
| 地名闸门 `_name_related` | `amap_client.py` | 解析出的省/市/区必须与输入有包含关系，否则按失败处理。高德会把垃圾输入模糊匹配到别处（`不存在的城市名xyz`→北京烟袋斜街），宁可空池报「候选不足」也不给错数据 |
| POI 归属过滤 `_poi_in_city` | `amap_client.py` | 过滤非本市候选（按 adcode 前缀，直辖市用省级 2 位前缀，否则 4 位），宁可候选为空报「候选不足」也不给外省数据 |
| 非场地类剔除 `_drop_non_venues` | `amap_client.py` | 「古北水镇(公交站)」「崇明秀山路」坐标齐全但不可能当场地；按高德**大类前缀**（`交通设施服务`/`道路附属设施`）剔除，不能靠猜子串（「公交站」并不是「公交车站」的子串） |
| 出发地消歧 `geocode(city=, near=)` | `amap_client.py` | 无上下文时「陆家嘴/中关村/新街口」会落到外省村庄级点位（实测偏差 1964km）。现在先用目的地城市限定，全球解析时取 `place/text` 热度首位 |
| 通勤圈分级 `_rank_pois(near=)` | `nodes.py` | 「超 100km 应降级」过去只是 prompt 里的一句散文，没有任何程序约束——实测「杭州团建」评分榜首是 138km 外的千岛湖植物园，照样排候选第一位。现在距离是排序键的第一位（降级不淘汰） |
| 交通路线基准 `_pick_route_anchor` | `nodes.py` | 交通报价必须基于**距目的地最近**的候选，否则「杭州一日团建」按千岛湖算里程，对应的是另一趟行程 |
| 目的地单一来源 `BasicInfo.destination_canon` | `schemas.py` | 四路提案只认这一份目的地，否则交通按「上海」算、流程按「莫干山」排，各说各话。`kind` 决定检索口径，`location` 是距离锚点（进 checkpointer 前转成普通 dict） |
| 空维度曝光 `CRITICAL_DIMENSIONS` | `nodes.py` | 场地/出发地坐标/路线是**必需**维度，「接口正常但 0 条」过去既不进 `trouble` 也不进 `unavailable`，整条链路上没人提到它——用户只看到「候选不足」，不知道为什么、该改什么。现在写进 `steps_log` 并出现在 `/plan` 的 `data_status` 与审批页 |
| 时间解析 `extract_time_ranges` | `nodes.py` | 只取首个正则匹配会让 `2026-10-18 09:00-12:00` 被 `26-10` 截胡；现遍历全部匹配并跳过日期形态 |
| 空明细拦截 | `nodes.py::reconcile` | 任一路提案 `items` 为空都算冲突（原来只查 venue，餐饮空壳能混过预算校验） |

对应回归测试：`backend/tests/test_destination_resolution.py`（27 项，mock 掉高德 REST，不触网）
+ `backend/_diag/verify_fixes.py`（54 项，纯离线为主，不调 LLM）。

## 🧯 失败分类与处置（改动前请先读）

外部调用失败以前只有两种表达——**空列表**或 **500**，于是"接口挂了"和"真没数据"、
"模型填错参数"和"网络抖动"全都长一个样。现在失败是有类型的：

| 失败类型 | 判定 | 处置 |
|---|---|---|
| 参数格式错 / 缺必填字段 | `ModelOutputError` | Schema 拦截；把「哪个字段、收到什么」回给模型重填（`invoke_structured`） |
| 正常业务结果（没搜到 / 候选不足 / 查不到该 POI） | `CallStatus.EMPTY` | **不当错误**：如实说明并交给下游决策，不重试、不报警 |
| 临时故障（超时 / 限流 / 5xx / 连接失败） | `TransientError` | 有上限重试 = **退避 + 次数 + 总时长预算**（`call_external`，三件缺一不可） |
| 能力未接入（没配 BOCHA/JUHE key） | `CapabilityDisabled` | `CallStatus.DISABLED`：刻意降级，不是故障，流程照跑并显式标注 |
| 必需配置缺失（没配 AMAP/LLM key） | `ConfigError` | **不降级**，直接失败——否则下游会拿常识编出一份看不出问题的假方案 |
| 程序缺陷 / 未知异常 | `classify() → None` | **不吞**：原样抛出 → 打全栈 → 500 |

**核心规则**：边界上**只捕 `AppError`**，其余异常一律让它冒出来。
这条规则让"程序缺陷不吞"变成机械可执行的，不用逐处判断。

关键位置：

- `app/graph/errors.py` — 异常层级 + `classify()` + `CallStatus`/`CallResult` + `call_external`
- `app/graph/nodes.py::gather_info` — 五路采集各自结算成败（**一路挂不再掀翻其余四路**），结果写进
  `state["data_status"]`（每路 status/reason/attempts/elapsed），降级维度显式写进 `steps_log`
- `app/graph/nodes.py::_proposal_node` — 四路提案共用一个外壳：**一路挂只废这一路**
  （四路是同一批 Send，旧行为是谁抛异常整批带着异常结束，已跑完的另外三路连钱带结果全部作废）。
  降级边界与上表逐条对齐：`ConfigError` 抛（四路全空只会拼出一份"看着完整"的空壳）、
  `AppError`/`ValidationError`/`CapabilityDisabled` 只废这一路、其余异常原样抛出
  （**切片形状不对是程序缺陷，不许被降级成"这维没数据"**）。
  降级提案的文案与"候选不足"分开写：前者是「这一维坏了，该重试」，后者是「确实没合适的选项」
- `app/graph/nodes.py::_proposal_sends` — 四路 Send 切片经 `_XxxPayload` **类型化**后再
  `model_dump()`：生产端写错键名/多写键当场抛，消费端用属性访问。同一个键名 `destination`
  以前在四路里装的是不同的东西（交通路装用户原话、场地路装归一展示名、餐饮路一个都没拿到），
  现在四路共用一个 `_Destination` 包（`raw`/`display`/`canon`/`place`），各取一面
- `app/graph/model.py::invoke_structured` — 网络层与 schema 层**分开计数、分开记日志**；
  网络层退避重试，schema 层回填字段级修正指令；两层共享同一个总时长预算
- `app/api/errors.py` — HTTP 映射：503（上游抖动，可重试）/ 502（坏响应）/ 501（能力未启用）/ 500（服务配置错）

对应回归测试：`backend/tests/test_error_handling.py`（标准库 `unittest`，**全部离线 mock**，
不触网也不真等——退避与预算用假时钟验证）。另有 `test_input_validation.py`（输入层边界/日期归一/
结构化分支零 LLM）与 `test_pref_aggregation.py`（headcount 计数口径/同义归一/规则提示零 LLM/已删字段）、
`test_proposal_isolation.py`（四路失败隔离的降级边界/切片类型与目的地打包/回炉不覆盖与定向重派/
餐饮路的统一纪律，全程替身接管 LLM 与搜索工具）。

```bash
cd backend
python -m unittest discover -s tests -v
```

## 🔧 目录结构

```
backend/
├── app/
│   ├── api/routes/
│   │   ├── event.py            # 团建策划 API(解析/偏好/plan/resume)
│   │   ├── map.py, poi.py      # 通用地图工具
│   ├── graph/
│   │   ├── graph.py            # build_event_graph(checkpointer, store)
│   │   ├── nodes.py            # 14 个节点(parse/reconcile/risk/approval/checklist/memory...)
│   │   ├── state.py            # EventPlanState(TypedDict + reducer)
│   │   ├── lexicon.py          # 活动/忌口同义词表(归一, 唯一真源; 前端有对应镜像)
│   │   ├── model.py            # LLM 封装(bind_tools 结构化输出)
│   │   └── amap_client.py      # 高德 REST 封装 + 进程级缓存
│   ├── models/schemas.py       # 全部 pydantic 模型
│   └── prompts/templates.py    # 提示词模板(需求/四路提案/冲突反馈/风险/checklist)
├── evals/
│   ├── golden_cases.json       # 5 个金标准评测用例
│   └── run_evals.py            # 评测运行器(程序断言 + LLM-as-judge)
├── scripts/verify_model_phase.py  # 全量功能验证脚本
└── data/                       # checkpoints.db(HITL) / memory.db(团队记忆)
frontend/                        # Vue3 + TS + Vite(表单/生成/审批/终稿 四页)
```

## 🔄 前端 API 适配记录

> 前端已按下表完成改造（`frontend/src/views/` 四页 + `services/api.ts`）；旧接口均已删除，调用会 404。本节保留作为接口演进记录。

### 已删除的旅行规划接口（→ 新接口对照）

| 旧接口（旅行规划，已删除） | 新接口（团建策划） | 迁移说明 |
|---|---|---|
| `POST /api/trip/plan`（一次性生成旅行计划） | `POST /api/event/plan` → `POST /api/event/resume` | 改为**两段式**：先发起拿 `thread_id`+`approval_payload`，审批后 resume；响应结构完全不同（见下） |
| `GET /api/trip/health` | `GET /api/event/health` | 节点清单改为 14 节点，新增 `checkpointer`/`store` 字段 |
| 旅行计划模型 TripPlan/Attraction/Hotel | `EventPlan`(proposals/conflict_points/risk_report/checklist) | 响应数据结构全部重构，无字段级映射 |

### 保留但与策划流程配套的接口

| 接口 | 变更 |
|---|---|
| `GET/POST /api/map/*`、`/api/poi/*` | **接口形状保留**，实现已从「MCP + TODO 桩」换成高德 REST 直连：`/api/map/poi`、`/api/map/weather` 现在返回真实数据（原先恒为空数组），`POST /api/map/route` 不再必然 500（原先 `plan_route` 返回 `{}` 触发 pydantic 校验错误），算不出路线时返回 `success=false` + 说明 |

### 新增接口

| 接口 | 请求 | 响应要点 |
|---|---|---|
| `POST /api/event/parse` | `{raw_text, team_id?}` | EventRequest；缺人数/日期/出发地 → **400** + `detail.missing_fields`（驱动表单补全） |
| `POST /api/event/plan` | `{people_count, date, origin, ...}` | `PlanStartResponse{thread_id, status, approval_payload, message, data?}`；`status=pending_approval` 时渲染审批页。必填缺失/日期在过去/超长 → **422**（进不到图里） |
| `POST /api/event/resume` | `{thread_id, approved, feedback?}` | 同上；`approved=true` → `status=completed` + `data.checklist`；`approved=false+feedback` → 新一轮 `approval_payload` |

### 审批页 payload 结构（`approval_payload`）

```jsonc
{
  "proposals": {"transport": {...}, "venue": {...}, "flow": {...}, "food": {...}},  // 四路报价
  "budget": {"total": 29790, "by_category": {...}, "budget_limit": 30000, "over_budget": -210},
  // 规则生成的偏好提示(非 LLM): 分歧 + 忌口约束混在一个列表里, 措辞自带区分
  "conflict_points": [
    "5人想爬山 vs 5人想剧本杀，建议分两组或都安排",
    "体能分层：1-2级 5人 vs 4-5级 5人，建议按强度分组安排（占已表态 10 人）",
    "忌口约束：3人素食、1人清真（餐饮方案需避开）"
  ],
  "risk_report": {"level": "low", "flags": [], "alternatives": [], "summary": "..."},
  "conflict_report": {"over_budget": 0, "conflicts": [], "suggestions": []},
  "user_feedback": []                                   // 历次否决意见
}
```

### checklist 结构（审批通过后 `data.checklist`，前端渲染可勾选清单）

```jsonc
{
  "assignments": [{"role": "集合签到", "owner": "待指定", "detail": "..."}],
  "purchase_list": [{"item": "横幅", "quantity": 1, "est_cost": 120, "note": "..."}],
  "notice_text": "可直接发群的文案..."
}
```

## 🧪 已知限制

- 天气预报仅未来 3-4 天（高德限制）：活动日超出范围时**不做天气风险判定**（避免拿无关日期的预报下结论），只在 `risk_report.summary` 里说明「天气无法评估，请临近复核」
- 偏好提交与 /plan 全流程的会话打通依赖 checkpointer，跨请求偏好暂存仍在内存（`_pref_store`，且是**进程级全局列表**，多会话会互相累加，仅适合单实例演示)
- 餐饮路的 LLM 搜索循环（最多 3 轮）在部分模型上仍会"只顾搜不做方案"，此时走 `_fallback_food_proposal` 程序化兜底（用已搜到的真实餐厅组报价并明确标注「程序化兜底」，需人工复核）
- 四路里某一路**降级**（这一维没数据）与某一路**确实没给明细**，在 `reconcile` 眼里是同一种"空明细"冲突：都会回炉、超限后交人工裁决。要区分得给 `Proposal` 加一个"本维状态"字段（改 schema + 重跑 `npm run gen-types`），暂留
- 出发地为区县/地标级地名（如「陆家嘴」）且目的地为另一城市时，消歧依赖 `place/text` 热度首位，极端情况仍可能选错并只打印告警；建议前端提示用户写「城市+区县」
- 偏好提示（`conflict_points`）虽然由 `aggregate_prefs` 在**四路提案之前**算出，但当前唯一消费方是审批页——四路提案的 prompt 并不读它。所以它对审批人是"事后说明"，不是分组/分流的决策依据；若要让偏好真正影响方案构成（例如先按偏好分组再报价），需要把消费点前移到提案前（偏好复核 `interrupt` 留作后续增强）
- checkpoint 反序列化存在 PrefSummary msgpack serde 警告（未来版本需注册 `allowed_msgpack_modules`）
- 付款/通知等不可逆动作的第二个 interrupt 为占位 TODO，未实现

## 📜 开源协议

[MIT](LICENSE)
