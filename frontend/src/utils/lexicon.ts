// 偏好词表与同义归一（backend/app/graph/lexicon.py 的镜像，用于输入时的即时归一）
//
// 为什么要在前端也折一遍：不改用户的输入习惯，但要让他**当场看到**"登山"被归成了
// "爬山"，而不是提交后在服务端悄悄改名。表以后端为准 —— 前端即使滞后，后端仍会折叠，
// 所以这里只影响用户看到的标签写法，不影响统计正确性。
//
// 规范词必须是活动名/忌口名，不是场地类别（别拿 backend 的 ACTIVITY_KEYWORDS 当源，
// 那张表的值是"风景区/餐厅"这种 POI 类别，会把不同活动并成一个桶）。

const ACTIVITY_SYNONYMS: Record<string, string> = {
  爬山: '爬山', 登山: '爬山', 徒步: '爬山', 户外徒步: '爬山',
  hiking: '爬山', 徒步旅行: '爬山',
  剧本杀: '剧本杀', 剧本杀推理: '剧本杀', 推理本: '剧本杀',
  密室: '密室逃脱', 密室逃脱: '密室逃脱',
  聚餐: '聚餐', 吃饭: '聚餐', 团餐: '聚餐',
  烧烤: '烧烤', bbq: '烧烤', 自助烧烤: '烧烤',
  露营: '露营', 露营烧烤: '露营',
  ktv: 'KTV', k歌: 'KTV', 唱歌: 'KTV',
  桌游: '桌游', 桌游馆: '桌游', 棋牌: '桌游',
  温泉: '温泉', 泡温泉: '温泉',
  骑行: '骑行', 自行车: '骑行', 骑车: '骑行',
  飞盘: '飞盘', 极限飞盘: '飞盘',
  漂流: '漂流', 越野: '漂流',
  滑雪: '滑雪', 滑雪场: '滑雪',
  古镇: '古镇', 古镇游: '古镇',
  cs: '真人CS', 真人cs: '真人CS', 真人镭战: '真人CS',
  拓展: '拓展训练', 团建拓展: '拓展训练', 拓展训练: '拓展训练',
}

const DIETARY_SYNONYMS: Record<string, string> = {
  素食: '素食', 吃素: '素食', 纯素: '素食', 全素: '素食',
  vegan: '素食', vegetarian: '素食',
  清真: '清真', halal: '清真', 穆斯林: '清真',
  不吃辣: '不吃辣', 忌辣: '不吃辣', 不吃辣椒: '不吃辣',
  海鲜过敏: '海鲜过敏', 不吃海鲜: '海鲜过敏', 海鲜忌口: '海鲜过敏',
  不喝咖啡: '不喝咖啡', 忌咖啡: '不喝咖啡', 不喝奶茶: '不喝咖啡',
  不吃牛羊肉: '不吃牛羊肉', 不吃牛羊: '不吃牛羊肉', 不吃牛肉: '不吃牛羊肉',
  乳糖不耐: '乳糖不耐', 不吃乳制品: '乳糖不耐', 不喝牛奶: '乳糖不耐',
  坚果过敏: '坚果过敏', 花生过敏: '坚果过敏',
  不吃香菜: '不吃香菜', 忌香菜: '不吃香菜',
}

function fold(tags: string[] | undefined, table: Record<string, string>): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const raw of tags ?? []) {
    const key = String(raw).trim()
    if (!key) continue
    const canonical = table[key.toLowerCase()] ?? key
    // 去重很关键：一个人同时勾了"爬山"和"登山"，折叠后是同一个词，
    // 不去重就会把同一批人算两遍。
    if (!seen.has(canonical)) {
      seen.add(canonical)
      out.push(canonical)
    }
  }
  return out
}

export const foldActivities = (tags: string[] | undefined) => fold(tags, ACTIVITY_SYNONYMS)
export const foldDietary = (tags: string[] | undefined) => fold(tags, DIETARY_SYNONYMS)

/** 从规范词取的建议项（保证"模板里能选的"一定能被归一） */
const toOptions = (table: Record<string, string>) =>
  [...new Set(Object.values(table))].map((v) => ({ value: v, label: v }))

export const PREF_SUGGESTIONS = toOptions(ACTIVITY_SYNONYMS)
export const DIETARY_SUGGESTIONS = toOptions(DIETARY_SYNONYMS)