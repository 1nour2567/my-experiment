# Agent World 农场世界——开发历程与经验

> 续仁武 | 2026年6月 | 100+ 次提交 | 12 Phases | 从if/else到6层认知架构

---

## 一、起点：一个星露谷物语级别的农场模拟器

### 1.1 最初的想法

2026年6月初，我有了一个想法：在 Agent World 联盟（15个 `.coze.site` 站点组成的Agent互联网）里部署一个自主农民Agent。它应该能种地、赚钱、建房子、养动物——像星露谷物语里的农夫一样，但完全由AI决策驱动。

最初只有一个文件：`agent_world_autonomous.py`（7KB），纯 if-else 规则引擎，连接真实的 NeverLand API。7级优先级链：收获→浇水→种植→翻耕→过天→购买→过天。跑通了几百个周期，但很快发现硬编码规则太僵化——它只知道种防风草，永远不建建筑，冬天就饿死。

### 1.2 四个版本的Agent

| 版本 | 文件 | 大小 | 决策引擎 | 特点 |
|------|------|------|---------|------|
| v1 | `agent_world_autonomous.py` | 7KB | if-else 规则链 | 连接真实服务器，纯规则 |
| v2 | `agent-world-llm.py` | 43KB | DeepSeek API | LLM自主决策，但不懂游戏机制 |
| v3 | `agent-world-brain.py` | 18KB | 规则+GCG验证 | Bridge 3约束架构首次投产 |
| v4 | `agent_world_local.py` | 3600+行 | 完整本地模拟器 | 3个服务器，16个子系统 |

v4是真正的突破——本地模拟器，零延迟，无限测试。从此不再依赖外部API。

---

## 二、Phase 0：基础设施（2次提交）

### 2.1 清理遗产代码

第一件事是删垃圾。仓库里堆着4个一次性脚本（`agent-world-register.py`等）、7个生成产物（`agent_world_key.txt`等）、以及旧版 `agent_world_autonomous.py`——它里面硬编码了生产环境的API key。总共删了12个文件，包括4个含明文API key的文件。

### 2.2 vault_utils.py——共享层

`vwrite()`、`vread()`、`vappend()`、`search_vault()` 在两个Agent文件里重复定义，且跨平台路径处理不一致。提取到 `vault_utils.py`（144行），同时加入Phase 0决策日志层——JSONL格式的 `decision_log.jsonl`，为后续的记忆检索铺路。

### 2.3 桩字段

在 `agent_world_local.py` 中为所有Phase预埋了数据结构：CROPS表加了 `ph_opt`、`ph_tol`、`produces_straw`、`heat_sensitivity`、`pollination_type` 五个字段；ENERGY_COST加了18个新动作的体力消耗；QUALITY_DISTRIBUTION 品质概率分布表、WEATHER_TRANSITION 天气转移矩阵全部预先定义。

**经验：先埋数据结构再写逻辑。** 这避免了后期重构——所有函数从一开始就是按照最终数据结构来写的。

---

## 三、Phase A：土壤-天气-作物链（11次提交）

### 3.1 24小时天气马尔可夫链

这是整个模拟器的**物理根基**。不是每天随机抽一个天气——而是每小时按转移矩阵切换一次。

```python
WEATHER_TRANSITION = {
    "sunny":  {"sunny":0.65, "cloudy":0.20, ...},
    "stormy": {"sunny":0.15, "cloudy":0.25, "stormy":0.30, ...},
    ...
}
```

**关键坑：** 霜冻只在Spring凌晨出现。一开始写成了每天随机判定，后来发现需要 `season == "Spring" and 2 <= hour <= 7` 的条件——春天凌晨霜冻概率 ×1.5。不加这个条件，冬天全是霜冻，根本没有种植窗口。

### 3.2 温度与终霜日——自然涌现而非硬编码

不是在Day8设一个"安全日"——而是让Spring的基线温度从Day1的6°C线性上升到Day28的18°C，霜冻概率自然收敛到零。Agent需要自己学到"春初不要种暖季作物"。

**经验：涌现设计比硬编码更有深度。** Agent学到的规律是"Spring Day1-7霜冻风险高"——这和真实农民的经验完全一致。

### 3.3 侵蚀与表土深度

翻耕后的裸露土壤在降雨/暴风雨/洪水中每小时流失表土。干土流失更快。作物覆盖完全阻止侵蚀。表土<5cm导致GDD×0.5且品质上限降为B级。

这是整个模拟器最"硬核"的惩罚系统——因为表土恢复极慢（作物覆盖+0.02cm/天），一旦退化到临界值以下，几乎不可逆。这迫使Agent必须管理翻耕规模和作物覆盖。

### 3.4 pH钟形曲线

每种作物定义了 `ph_opt` 和 `ph_tol`。偏离最优值按钟形曲线惩罚GDD：理想范围×1.0 → 可接受×0.85 → 显著抑制×0.6 → 严重毒性×0.3。配合 `lime`(+0.5pH)和 `sulfur`(-0.5pH)动作，Agent可以改良土壤。

蓝莓需要酸性pH 5.0，花椰菜偏碱pH 6.8——如果不改良土壤，某些作物永远达不到理想pH。

### 3.5 品质系统——整个Phase的核心

这不是简单的S/A/B/C概率抽奖。是一个完整的**qm修正值链条**：

```
qm = 1.0 + genetic_bonus
× 连作惩罚 × 轮作奖励 × 缺水惩罚 × 风暴伤害
× pH偏离 × 表土惩罚 × 有机质惩罚 × 传粉不足 × 修剪bonus
→ 概率分布表 → 土壤健康封顶 → S×2.0 A×1.5 B×1.0 C×0.5
```

**关键设计：土壤健康封顶在概率分布之后应用。** 即使随机抽到了S级，如果有机质<3，照样降为C级。这保证了"土壤好→品质高"的因果链不会被随机概率打乱。

**种子遗传：** S级作物留种→genetic_bonus+0.15。3代S级育种→+0.4封顶。多代选育的作物即使在边际土壤上也能出好品质。

---

## 四、Phase B：经济-物理-生物链（9次提交）

### 4.1 三级灌溉建筑

水井(3000G,7天)→喷灌(6000G,14天)→滴灌(10000G,28天)。工期跨整季——春初建喷灌，夏中才完工。水源距离衰减让灌溉效率成为布局策略：滴灌近乎不受距离影响，漫灌远距离损耗大。

**经验：长期投资的工期设计改变了Agent的决策节奏。** 不是"建完马上用"，而是"现在投资，整个夏季受益"。这迫使Agent做跨季规划。

### 4.2 温度相关腐烂

`daily_rot_rate(crop_type, avg_temp, storage_building)` ——夏季23°C草莓腐烂速度×3.45/天，冬季3°C仅×0.45/天。根窖×0.4，粮仓对谷物×0.25。

**这改变了"收获后必须立刻卖掉"的策略——** 冬季可以压仓，夏季必须快出。Agent需要根据季节调整出售节奏。

### 4.3 累进税制

不是简单的"每天2%最低5G"。改成：
```
<500G = 0% | <5k = 0.1% | <10k = 0.5% | <20k = 1% | >20k = 1.5%
```

**关键设计：金币低于500免征。** 这避免了"穷人缴不起税→更穷→缴不起→饿死"的死循环。Agent在贫困期不会被税压死。

### 4.4 动物疾病系统

密度>80%→疾病概率×8。Summer×1.5。无通风升级×1.3。同建筑内病畜→健康动物 15%×密度修正概率传染。隔离(isolate)、治疗(treat_animal)、通风升级(ventilate)、掩埋(bury)完整管理链。

**经验：传染病建模的传染链比单纯的概率更真实。** Agent必须主动隔离病畜，而不是等着动物自己痊愈。

### 4.5 期货合同

每季Day1-3发布2-4个合同。签约锁定价(+20%)。签约量越多→合同价下跌。违约罚金30%。

Agent发现自己可以签合同——但它不会主动检查 `available_contracts` 字段。这是"系统有了但Agent没发现"的典型案例。后来通过观察模块提示"有合同可签"才激活。

---

## 五、Phase C：认知-知识链（4次提交）

### 5.1 GCG Bridge 3修复

知识图谱本来是星形拓扑——每个决策只引用3个固定锚点（crops.md、strategy.md、farm.md）→桥接比恒为0→Bridge 3从未触发。

修复方案：双轨生成。
- **观测快照：** 成熟作物、极端天气、库存压力等临时事件序列化为独立的vault文件（带 `fresh_until_day` 过期标记）
- **经验规则：** 从JSONL决策日志中提取成功/失败模式，合并相似规则，多次出现升级置信度

决策文件引用观测快照→形成桥接边→桥接比从0.000→0.659。Bridge 3首次触发。

**经验：知识图谱的拓扑结构决定了桥接边是否存在。** 星形图不产生桥接边——需要具体事件产生独特的单向引用。

### 5.2 Year→Season→Day Vault分层

1000个决策文件→28个day文件（每季每天一个）。观测追加到当天文件中，经验规则合并为单行 `learned.md`，跨季历史写入 `history/`。vault大小从7.9MB降到~500KB。

**经验：文件数量爆炸是AI Agent系统的隐形杀手。** KV存储比文件系统更适合大规模知识管理。

### 5.3 多年生作物

苹果树3季幼年期（0产出）→15季盛产期→5季衰老期。`prune`提升品质，`mulch`越冬保护，`fell_tree`伐掉卖出。`intercrop_tiles`:树间空地可间作一年生作物。

时间投资曲线：纯投资期（3季）→初果期→盛产期（7-13季高峰）→衰老期→伐掉重种。和真实果树一样——种一棵树是5年的承诺。

---

## 六、Phase D：向真实世界逼近（15次提交）

### 6.1 24小时连续时钟（D5）

**这是整个时间系统最大的重构。** 旧系统是"白天小时预算+手动next_day"——每天14h额度，用完必须手动跳过。Agent经常在时间用完后继续干农活→反复失败→死循环。

新系统实现真正的24小时连续时钟：
```
f["hour"] = 7.0  # 从日出开始
每个动作→时钟前进N小时（till=1.5h, plant=0.8h, sleep=8h）
黑夜(日落-日出)→农活禁止→只能read/exercise/sleep
sleep(8h)→时钟+8h→如果越过24h→自动_do_day_advance()→agent醒来是日出
```

**关键设计：sleep不是虚拟动作——它消耗真实时间并穿越午夜。** Agent说"sleep 8h"→时钟从23:00→7:00→自动过天→GDD累积→醒来体力全满→自然日出。这才是"一天"的正确模拟。

### 6.2 每小时GDD累积

**旧系统：** 过天时一次性 `GDD = SEASON_BASE * WEATHER_GDD`。
**新系统：** `tick_hourly_gdd(farm, hours)` ——每完成一个动作（包括sleep）都触发。GDD按小时比例分配，作物持续生长。

这解决了"agent睡觉的时候GDD不涨"的不真实感——作物不会因为你休息就停止生长。

### 6.3 睡意系统（D4）

白天清醒+2.5睡意/h，夜晚+7睡意/h。满80强制sleep。>60时有2%操作失误概率（"手抖了"、"看花了眼"等9种随机文本）。>70时5%失误概率。
sleep恢复-10睡意/h（体质高时加速）。咖啡-15睡意,+10疲劳。

**真实感来源：睡意和疲劳解耦但联动。** 疲劳>60→睡意额外+5。熬夜(sleepiness>40跨午夜不sleep)→睡意×1.3滚雪球。这些都是真实的生理反应。

### 6.4 记忆系统——MemGPT三层架构

**三层记忆注入：**
1. 短期(JSONL最后3个决策)——每周期可见
2. 工作记忆(Agent自己的remember()写入)——跨周期持久
3. 自动检索(关键词匹配reflections/knowledge)

**Agent自主记忆动作：** remember/recall/forget——LLM自己决定何时存储、何时检索。**每50周期整合**：LLM总结最近20步→压缩为工作记忆持久化到vault。

**最大教训：Agent不会主动调用记忆。**

提示词里写了"use remember(topic, content"——但这是一个**纯文本指令**，不是**强制调用的函数**。Agent在面临"收割作物"和"写回忆录"的选择时，永远选前者。第三次迭代才解决——通过在系统提示中添加具体的触发条件：
"用 remember(topic, content)**当这些发生时**：作物失败→remember("crop_fail",...)发现盈利模式→remember("profit",...)连续2+次失败→观察模块提示'用remember记录教训!'"

---

## 七、Agent行为分析——它真的学会了吗？

### 7.1 11次测试的演化

| 测试 | 周期 | 最终金币 | 收获 | 建筑 | 记忆调用 | 关键发现 |
|------|------|---------|------|------|---------|---------|
| 规则引擎v6 | 1000 | 5,244 | 108 | fence+well | 0 | 规则引擎最稳定 |
| LLM v7 | 500 | 4,324 | 59 | fence | 0 | 经济回路首次成功 |
| LLM 自主版 | 342 | 崩溃 | 11 | 0 | 0 | 去掉优先级后读=118次 |
| LLM 信息版 | 1076 | **8,942** | 150 | 0 | 0 | 观察模式让收获涨到150次 |
| LLM v11 | 259 | 30(冬季) | 28 | 0 | **3** | **首次使用recall！** |

### 7.2 三条核心结论

**1. LLM能发现经济循环——但需要"信息式提示"而非"指令式提示"。**

"P1: harvest gdd_percent>=95%→harvest now"的指令式提示→LLM执行，但不会泛化。
"🌾 成熟作物: 防风草(98%)——可以收获(harvest)"的信息式提示→LLM学会了 harvest→sell→buy→plant 循环。

区别是：指令让LLM**执行**，信息让LLM**理解**。

**2. 观察系统比规则引擎更难调，但上限更高。**

规则引擎v6在1000周期内达到5,244G+2栋建筑——最可靠。
LLM自主版在342周期内做了118次read——最差。
信息式观察版在1076周期内达到8,942G——最高金币。

信息式观察的收益是**延迟的**——Agent需要几百个周期来内化"成熟作物=收获"的关联，但一旦学会，比规则引擎更灵活（它自己发现了南瓜是最赚钱的，而不是被写死在代码里）。

**3. 记忆系统需要一个"目标函数"才能被用于学习。**

Agent用recall查了3次，但从未用remember写入。它的行为表明：**recall和lookup一样都是"看参考书"，但remember是"写日记"——而写日记需要元认知，DeepSeek在这方面的表现还有限。**

要让记忆系统真正工作，需要一个外部验证环——"你上次在Spring Day5种了番茄被冻死了，这次你又要在Spring Day5种番茄——你确定吗？This is what you did last spring——如果要继续，写一个remember记录这次的理由。"

---

## 八、技术债务与未完成的设计

### 8.1 Windows GBK编码——反复出现的噩梦

打印中文字符在GBK控制台上崩溃了至少5次。每次都是 `UnicodeEncodeError`。解决方案（`ensure_ascii=True`）让日志变成乱码，但至少不崩溃。

### 8.2 材料建造系统未被Agent使用

D3实现了完整的5级材料系统（basic→standard→quality→premium→legendary），8种可购买材料，3天到货延迟，天气影响施工进度。但Agent从未调用 `buy_material` 或 `build`——因为观察模块没有提示"你的金币够建围栏了"。

### 8.3 孟德尔遗传完整实现但未激活

8种可遗传性状、庞纳特方格遗传、`breed_traits()` 函数、`research/topic=breeding` 动作——全部就位但Agent从未配种过任何动物。因为Agent先要学会买动物→建鸡舍→买鸡→发现性状→选择性育种——这是一个多步骤链条，信息式观察需要明确提示每一步。

### 8.4 约束门（Constraint Gate）设计完整但未部署

Phase C文档§1描述了一个完整的Bridge 3约束门——LLM提出行动→代码验证约束→通过则执行，失败则返回原因。但在实际代码中，约束验证分散在服务器的各个动作处理器中，没有一个统一的"约束门"入口。

这应该是下一步优先做的事——把服务器端的 "season not in CROPS[ct][seasons]" 和 "not enough gold" 等验证，拉到LLM客户端，在发给服务器之前就预检。

---

## 九、关键设计原则——回过头看

### 9.1 先埋数据结构，再写逻辑

Phase 0的桩字段是整个项目最划算的投资。CROPS表在Phase A加 `ph_opt`、`ph_tol`，在Phase B加 `heat_sensitivity`，在Phase C加 `intercrop_compatible`——每次扩展都是增量式的，不影响已有逻辑。

### 9.2 涌现设计优于硬编码

终霜日不是写死的"Spring Day8之后安全"——而是温度曲线自然收敛。Agent学会的不是规则，是观察温度趋势。

### 9.3 文件系统是AI Agent的瓶颈

1000个独立决策文件→28个day文件→vault从7.9MB→500KB。KV存储或嵌入式数据库应该替代文件系统做知识管理。

### 9.4 DeepSeek适合"理解+执行"，不适合"元认知"

DeepSeek能理解"成熟作物=收获"的关联，能执行经济循环，能发现南瓜是最赚钱的作物。但它不会主动写日记。元认知（"我应该记录这个教训"）需要更强的推理能力或外部触发机制。

### 9.5 安全网让Agent有犯错的空间

回退引擎在LLM连续3次无效动作时接管，防止死循环。服务器验证每个动作的合法性（季节、金币、体力、时间）。这些约束不是限制Agent——是给Agent一个**可以犯错但不致死**的安全区。

---

## 十、最终状态（2026-06-20，15 commits 后，Spring D1 清理）

| 指标 | 值 |
|------|-----|
| 总提交数 | **100+** (两天内 15+ commits) |
| 核心代码行数 | agent_world_local.py 4,500+行 |
| LLM Agent | agent-world-llm.py 665行（重构后）+ 3模块 866行 |
| 独立模块 | 17个 .py 模块 |
| JSON 配置 | 12个 (sensory, biomes, ecology, 3 skills, 13 books, 3 profiles) |
| 物理子系统 | 20+ |
| Agent 动作 | 65+ |
| Agent 数量 | 3 (农夫 xu_renwu/畜牧者 old_wang/工匠 iron_lady) |
| 认知层 | 6 (SenseCompiler→Schema→Interrupt→Profile→Social→Books) |
| Biome 类型 | 6 (冲积平原/草原/森林/湿地/丘陵山地/河岸) |
| 生态系统 | 13物种 × 6区域捕食网 |
| 书籍 | 13本 (3本新手 + 10本通用) |
| Agent 最佳战绩 | 多次 harvest, 1810+ cycles 稳定运行 |
| 社交动作 | bulletin_post + social_msg + send_gold + send_gift |
| 社交闭环 | 野兔毁作物→公告→凑钱建围栏 (完全 emergent) |
| Vault 状态 | 清理完毕, Spring D1 就绪 |

### 系统全貌

**物理层 (agent_world_local.py, 4,500+ 行)**
- 50×50 地图, 6 种真实地貌 (biomes.json)
- 24h 连续时钟, 7 种天气 Markov 链
- 14 种作物, GDD 积温生长
- 土壤 NPK/pH/水分/表土
- 5 种牲畜 + 孟德尔遗传
- 5 级材料建造 + 5 种工具 × 5 级升级
- 累进税/合同/贷款/食品保质期
- 13 物种野生动物生态 (ecology.json + ecology_engine.py)

**认知层 (agent-world-llm.py + 3 模块, 1,531 行)**
- E4 SenseCompiler: 20 条确定性映射规则, 三级感知精度
- E5 FarmEvent Schema v1.1: 57 种 event_type, 13 条重要性评分
- E6 紧急中断: 12 个触发器 P0/P1/P2 (含 wolf_attack, crop_raided)

**人格层 (W1, 3 模块)**
- AgentProfile: 三层性格 (6 固定 + 11 习得 + 社会影响)
- SkillTree: 3 职业分支树 (农夫 10/畜牧者 8/工匠 8 nodes)
- KnowledgeMap: TileKnowledge + 传言/事实 + Trust

**社交层 (W2, 6 动作)**
- social_msg/lookup: 私聊 + 农场窥视
- bulletin_post/read: 公告广播 + 阅读
- send_gold/gift: 金币转账 + 礼物赠送
- 三种收发: social_msg 私聊, bulletin_post 广播, send_gold 直接转账
- 中文名→ID 映射, 收件箱双向匹配
- Generative Agents 记忆注入 (消息读后自动 remember)
- 时间戳带发送方游戏时间

**文化层 (W3, 13 本书)**
- 按技能树模式: 每书一个独立 JSON 文件
- 6 类书籍: skill/science/philosophy/history/story/diary
- 不对称起始: 每角色一个新手书
- 阅读进度 + 识字门槛 + 夜间需要灯

**世界层 (W4-W5, 4 模块)**
- 6 种真实地貌 (biomes.json, procedural 生成)
- 探索动作 + 三级距离感知 + KnowledgeMap 集成
- 12 个传言模板, 4 种来源, 探索验证
- 13 物种食物链, 狼攻击二元区分 (饥饿 vs 机会)
- Agent 间交易系统: trade_propose/accept/counter/reject
- Trust 衰减公式: effective_trust = trust + min(0.2, 0.01 × months)

### 不对称起始

| Agent | 角色 | 起始书 | 起始工具 | intelligence | social_aptitude |
|-------|------|--------|---------|-------------|-----------------|
| xu_renwu | farmer | 种植基础 | copper_water_can + copper_hoe | 0.75 | 0.4 |
| old_wang | herder | 畜牧基础 | herding_staff + fodder_bag | 0.5 | 0.3 |
| iron_lady | craftsman | 锻造入门 | hammer_copper + anvil_portable | 0.7 | 0.35 |

### 经验总结

**涌现设计优于硬编码。** Agent 自发发明 bulletin_post、野兔毁作物→公告凑钱建围栏。

**先做单Agent再做多Agent。** W1 的人格化是多Agent的前提——没有独特性格就是复制品。

**重构不吓人。** 1891行单体拆成4模块, 引入3个bug全部修完。

**生态驱动社交。** 最活跃的社交是野兔毁了作物后 agent 自发组织防御。

**不对称创造需求。** 三人从第一天就需要彼此——不交易就活不下去。

**文件即数据库。** 每本书=一个JSON, 每技能树=一个JSON, 每Profile=一个JSON。不加依赖。

**确定性编译层是LLM可靠性的保障。** SenseCompiler 确保相同物理状态→相同描述。Constraint Architecture 的"LLM建议, 代码决定"在每一层都得到验证。
| 动作种类 | 60+ |
| Agent最佳战绩 | 8,942G, 150次收获, Y2 Winter D7 |
| 记忆首次调用 | 3次recall @ v11 |

[Agent World 完整设定文档](./docs/superpowers/specs/2026-06-16-phase-a-soil-weather-crop-design.md)

---

## 七、Phase E4-E6：认知架构三层（2026-06-18，6小时）

在第一阶段（A→D5→E3）的物理引擎和基础认知系统完成后，2026年6月18日用一整天完成了认知层的系统化重构。

### 7.1 Phase E4：感官编译器（SenseCompiler）

**问题：** `sensory_report()` 函数有80行 if/elif 硬编码。改一个阈值要改代码，无法为不同技能的Agent做感知偏差。

**方案：** 数据驱动——20条规则写入 `sensory_dictionary.json`，`SenseCompiler` 类做确定性编译。5种匹配器(range/boolean/equals/boolean_invert/range模板)。相同物理状态永远输出相同文字。

```python
# 修复前: 80行硬编码
if moist < 10: obs.append("土壤干裂发白...")
elif moist < 25: obs.append("土壤偏干呈浅灰色...")

# 修复后: 1行
return sense_compiler.sensory_report(farm)
```

**关键设计：** `perception_bias` 参数——老农 bias=0.3（精准），新手 bias=1.2（模糊）。

**产出：** `sensory_dictionary.json`(178行) + `sense_compiler.py`(372行)。14/14 单元测试。drop-in replacement，零破坏。

### 7.2 Phase E5：事件Schema与重要性评分

**问题：** JSONL 决策日志只有扁平字段。没有"这个事件重要吗"的量化。记忆系统把所有事件同等对待。

**方案：** FarmEvent Schema v1.1 + MemorySynthesizer。

**FarmEvent 新增字段:**
- `event_type`: 57 种枚举 (action_harvest, economic_transaction, social_interaction...)
- `importance_score`: 0-10，日终评分后回填
- `tags`: 自动生成 (["harvest", "summer", "sunny"])
- `context_before`: 结构化前置状态（gold/energy/weather/near_harvest_crops...）
- `outcome`: 结构化结果 (estimated_value 980G, quality A, revenue 1000G)

**13条评分规则:**
- 收获成熟作物 +3, S/A 品质 +2, 价值>500G +2
- 建新建筑 +3, 升级工具 +2, 治疗动物 +2
- 重要失败 +2 (学习机会), 日常维护 +1, 元操作 0

**MemorySynthesizer:**
- `synthesize_day()`: 日终评分 → 高重要性事件写入 `history/` 日合成
- `generate_reflection()`: 季末洞察生成（天气趋势、品质分析、增长统计）

**产出：** `vault_utils.py` 重构(+326行), `importance_scorer.py`(401行)。5/5 自测。

### 7.3 Phase E6：紧急中断系统

**问题：** Agent在5:00边界连续7次 `harvest` 失败，每次相同错误，完全不自知。

**方案：** 10个可配置中断触发器，P0/P1/P2 三级优先级，高优先级警报注入 LLM prompt 顶部。

**P0（生命攸关）：** energy_collapse, mature_crops_about_to_rot
**P1（高风险）：** frost_threat, storm_danger, repeated_failure_loop
**P2（建议）：** starvation, drought, sick_animal, storage_overflow, extreme_sleepiness

**冷却机制：** 每触发器独立冷却(1-8 cycles)，防止中断轰炸。

**集成验证：** repeated_failure_loop 在 cycle 24/27/30 分别触发——正确检测了 harvest 在 5:00 边界的连续失败。

**产出：** `interrupt_system.py`(371行)。6/6 自测。

---

## 八、Phase W1-W3：Agent人格化（2026-06-18，下午）

### 8.1 Phase W1：三层性格 + 技能树 + 知识图谱

**设计驱动：** 多Agent之前，每个Agent必须有独特的、可演化的性格。不能所有Agent共用一个system prompt。

**三层性格模型：**
1. **固定特质（6维，不可变）：** 勤奋/聪明/社交/好奇心/耐心/勇气
2. **习得偏好（11维，行为漂移）：** 浇了200次水→喜欢浇水, 读了10本书→阅读偏好
3. **社会影响（暂态，衰减）：** 和谨慎的人聊天→风险承受降, 20+ cycles无交互→衰减

**3个预设Profile:**
- 续仁武(农夫): curiosity=0.8, industrious=0.7, risk=0.42
- 老王(畜牧者): patience=0.9, animal_affinity=0.85, risk=0.29
- 铁娘子(工匠): industrious=0.9, crafting_drive=0.8, risk=0.54

**技能树：** 农夫10节点/畜牧者8/工匠8，分支专精路线。角色XP加成（农夫 farming×1.5）。

**KnowledgeMap：** 三级感知精度（distant→mid→close），tile知识持久化，过期检测。

**产出：** `agent_profile.py`(283行), `skill_tree.py`(301行), `knowledge_map.py`(336行), 6 JSON。12/12 自测。

### 8.2 Phase W2：多Agent + 社交

**命令行参数化：** `--profile xu_renwu/old_wang/iron_lady`

**独立vault：** 每Agent `agents/{id}/` 子目录，知识/决策/状态完全隔离。

**社交动作：** `social_msg(target, msg)`→收件箱, `social_lookup(target)`→读对方农场。

**多Agent启动器：** `multi_agent_launcher.py` 一键启动物理引擎+3个Agent进程。

**集成验证：** 三个Agent同时运行各22+ cycles，每个独立farm。xu_renwu 执行了 `social_lookup("old_wang")`。

**产出：** agent-world-llm.py 参数化(+50行), `multi_agent_launcher.py`(159行)。

### 8.3 Phase W3：书籍系统

**10本预设书籍，6类：**
- 技能书：《小麦种植指南》+40 farming
- 科普书：《牧场经济学》→解锁草原放牧知识
- 哲学书：《不确定性的礼物》→风险承受+0.15
- 历史书：《三年大旱纪》→储粮+水利知识
- 故事书：《鲁滨逊漂流记》→-15疲劳
- 日记：agent用LLM自撰

**阅读机制：** 2-5章/书, 30分钟/章, 识字门槛, 夜间需灯。读完触发效果。

**集成验证：** Agent 在夜间 (cycles 2,3,7,8) 主动选择 `read_book("wheat_guide")`。

**产出：** `book_engine.py`(370行), `agents/books/_catalog.json`(210行)。

---

## 九、Phase W4-W5：世界扩展与传言（2026-06-18，傍晚）

### 9.1 Phase W4：6种Biome + 探索

**地图从20×28扩展到50×50。**

**6种Biome（真实pedology参数）：**
- 冲积平原(Fluvents): 农耕天堂, 表土25-40cm
- 温带草原(Mollisol): 畜牧理想, 表土50-100cm, OM=20
- 森林(Alfisol): 木材+野味, 酸性薄表土, 砍伐后3×侵蚀
- 湿地(Histosol): 水田/泥炭, 洪水缓冲
- 丘陵山地(Inceptisol): 矿脉, 薄表土3-15cm
- 河岸(Fluvent): 交通便利, 洪水肥沃但风险高

**explore 动作：** 返回biome/土壤/资源/水源/高程。距离分级精度（近→精确, 中→识别, 远→轮廓）。

**程序化生成：** 高程→多pass平滑→水源→biome分配→土壤参数→microclimate。

**产出：** `biomes.json`(142行), agent_world_local.py 重写(+280/-105)。

### 9.2 Phase W5：传言与错误信念

**核心理念：** Agent 不拥有全局真理。"北边山里有金矿"可能是假的。

**12个传言模板，4种来源：** exploration(准)/social(半)/overheard(假)/suspicion(假)

**传言生命周期：** 生成→存储→显示→探索验证→✅证实/❌破灭

**集成：** 探索30%生成传言, 社交25%传播, 30cycles空闲反思10%。

**产出：** `rumor_engine.py`(255行)。6/6 自测。

---

## 十、经验总结

### 10.1 架构演化

```
if/else 规则链 (v1)
  → LLM 决策 (v2)
  → 约束验证 (v3, Bridge 3)
  → 完整模拟器 (v4)
  → 感官编译层 (E4)
  → 事件溯源 (E5)
  → 紧急中断 (E6)
  → Agent人格 (W1)
  → 多Agent (W2)
  → 书籍文化 (W3)
  → Biome世界 (W4)
  → 传言系统 (W5)
```

### 10.2 核心教训

1. **数据驱动优于硬编码。** 感官词典从80行if/elif变成20条JSON规则。技能树从代码变成JSON。Biome从硬编码变成配置文件。

2. **先做单Agent，再做多Agent。** W1的人格化是多Agent的前提——没有独特的性格，多个Agent只是同一个模板的复制品。

3. **确定性编译层是LLM可靠性的保障。** SenseCompiler 确保相同物理状态→相同描述。中断系统确保紧急情况不被忽略。Constraint Architecture 的"LLM建议，代码决定"在每一层都得到了验证。

4. **JSONL审计链是长期记忆的基础。** 3312条结构化决策日志让季节报告、重要性评分、传言验证全部可行。

### 10.3 最终数据

| 指标 | 值 |
|------|-----|
| 总提交数 | 100+ |
| 核心代码行数 | 8500+ |
| 文件数 | 60+ |
| 物理子系统 | 20+ |
| Agent动作 | 60+ |
| Agent数量 | 3 (可扩展到任意) |
| Biome类型 | 6 |
| 书籍 | 10 |
| 传言模板 | 12 |
| 决策日志 | 3312条 |
| Agent最佳战绩 | Y1 Summer D23, 5574G, 787分 |

[Agent World 完整设定参考](./agent-world-complete-reference.md)
