# Agent World 农场世界 — 完整设定参考（Phase W5）

> 版本: Phase W5 | 2026-06-18 | 续仁武 | 12 phases, 60+ 文件, 8500+ 行

## 目录

**物理世界:**
1. [世界架构](#1-世界架构)
2. [24小时连续时钟](#2-24小时连续时钟)
3. [天气与温度](#3-天气与温度)
4. [土壤与地形（6种Biome）](#4-土壤与地形6种biome)
5. [作物系统](#5-作物系统)
6. [品质与遗传](#6-品质与遗传)
7. [农夫身体](#7-农夫身体)
8. [睡意系统](#8-睡意系统)
9. [体质与学识](#9-体质与学识)
10. [建造系统](#10-建造系统)
11. [工具系统](#11-工具系统)
12. [排水系统](#12-排水系统)
13. [牲畜系统](#13-牲畜系统)
14. [经济系统](#14-经济系统)

**认知层:**
15. [感官感知（SenseCompiler）](#15-感官感知sensecompiler)
16. [事件Schema与重要性评分](#16-事件schema与重要性评分)
17. [紧急中断系统](#17-紧急中断系统)
18. [记忆系统（MemGPT三层）](#18-记忆系统memgpt三层)
19. [季节报告与跨年学习](#19-季节报告与跨年学习)

**Agent人格化:**
20. [Agent身份与性格（三层模型）](#20-agent身份与性格三层模型)
21. [技能树系统（三职业分支）](#21-技能树系统三职业分支)
22. [个人知识图谱（KnowledgeMap）](#22-个人知识图谱knowledgemap)
23. [多Agent与社交系统](#23-多agent与社交系统)

**文化与世界:**
24. [书籍系统](#24-书籍系统)
25. [Biome地形系统](#25-biome地形系统)
26. [传言与错误信念](#26-传言与错误信念)
27. [探索与地图迷雾](#27-探索与地图迷雾)

**参考:**
28. [动作完整清单（60+）](#28-动作完整清单60)
29. [文件架构](#29-文件架构)
30. [API参考](#30-api-参考)

---

## 1. 世界架构

### 物理尺度
- **地图大小:** 50×50 = 2500 tiles（Phase W4扩展，原20×28）
- **六种 Biome:** 冲积平原(40%)、温带草原(20%)、森林(10%)、湿地(8%)、丘陵山地(12%)、河岸(10%)
- 地形由程序化生成：种子高程 → 多 pass 平滑 → 水源放置 → biome 分配

### 三个服务器端口
```
8080 — Agent World (注册/认证)
8081 — NeverLand Farm (农场物理)
8082 — AfterGateway Bar (社交酒馆)
```

---

## 2. 24小时连续时钟

- 每小时独立滴答，0.0–23.999 连续
- 日出/日落随季节变化: 春 6-20, 夏 5-21, 秋 7-18, 冬 8-17
- 天黑 → 只能 `read`/`exercise`/`sleep`/`read_book`/`social_lookup`
- `sleep(hours)` 可跨午夜 → 自动过天
- 日相系统: morning 5次动作 / afternoon 3次 / evening 1次

---

## 3. 天气与温度

- **七种天气:** sunny, rainy, stormy, drought, frost, heat_wave, flood
- Markov 链转移概率
- 日温正弦曲线 + 季温线性插值
- GDD（Growing Degree Days）积温：每作物独立 GDD 阈值
- 暴风雨 → 积水 + 土壤侵蚀；霜冻 → 幼苗冻伤

---

## 4. 土壤与地形（6种Biome）

### Biome 定义（`biomes.json`, 142行）

| Biome | 学术对应 | 表土深度 | 有机质 | 农耕适性 | 畜牧适性 | 采矿潜力 |
|-------|---------|---------|--------|---------|---------|---------|
| 冲积平原 | Fluvents | 25-40cm | 15 | 0.95 | 0.4 | 0.05 |
| 温带草原 | Mollisol | 50-100cm | 20 | 0.6 | 0.95 | 0.1 |
| 森林 | Alfisol | 8-20cm | 25 | 0.3 | 0.3 | 0.2 |
| 湿地 | Histosol | 10-25cm | 30 | 0.2 | 0.2 | 0.05 |
| 丘陵山地 | Inceptisol | 3-15cm | 5 | 0.15 | 0.4 | 0.7 |
| 河岸 | Fluvent | 15-35cm | 12 | 0.7 | 0.5 | 0.1 |

### 三种基础土质（biome内随机分配）
- **sand:** 持水 0.3, 排水 0.8, 耕作难度 1.2, 养分保持 0.4
- **loam:** 持水 0.6, 排水 0.5, 耕作难度 1.0, 养分保持 0.7
- **clay:** 持水 0.9, 排水 0.2, 耕作难度 0.7, 养分保持 1.0

### 土壤参数
- NPK 三维 + organic_matter + pH + soil_moisture (0-100%)
- 表土深度按 biome 的真实 pedology 参数生成
- 每 biome 有隐藏资源（iron_ore, peat, medicinal_herb, fish, clay...）

---

## 5. 作物系统

- **14 种作物:** wheat, potato, parsnip, cauliflower, corn, tomato, blueberry, strawberry, pumpkin, melon, winter_seeds, tulip, powder_melon, soybean
- GDD 小时制生长（0→100%），每作物独立 GDD 阈值
- 每日需浇水（rainy/flood 自动浇）
- 杂草系统：空地长杂草，抢养分
- 种子保存：S 级母本 +0.15 遗传分，3 代 S 封顶 +0.4

---

## 6. 品质与遗传

- S(×2.0价) / A(×1.5) / B(×1.0) / C(×0.5)
- 孟德尔遗传：显性/隐性性状
- 品质锁定：OM<3→C, 表土<5cm→最高B, pH 不对→lime/sulfur
- soybean 固氮（N +4/收割）

---

## 7. 农夫身体

| 属性 | 范围 | 衰减 | 说明 |
|------|------|------|------|
| ⚡ 体力 | 0-200 | 按动作消耗 | 0=无法行动 |
| 🍖 饥饿 | 0-100 | -12/天 | <20 能耗×1.5 |
| 💧 口渴 | 0-100 | -10/天 | <20 能耗×1.3 |
| 😴 疲劳 | 0-100 | -30/晚 | >80 能耗×1.5 |
| 😴 睡意 | 0-80 | +2.5/h白 +7/h夜 | ≥80 只能sleep |

---

## 8. 睡意系统

- 白天 +2.5/h, 夜晚 +7/h
- `sleep(hours)`: -10睡意/h, +35体力/h, -50疲劳
- sleep 可跨午夜 → 自动过天 → 醒来即日出
- Fitness 加速恢复: 体质 2.0 → 15/h

---

## 9. 体质与学识

- **Fitness:** 浮动值，exercise 提升，影响动作时间（最高 -40%）
- **Knowledge:** 7 技能独立评分:
  - farming, husbandry, machinery, processing, construction, scholarship, exploration
- read/research 提升学识

---

## 10. 建造系统

- 5 级材料: basic(原木+黏土)→standard(木材+砖)→quality(硬木+石)→premium(铁筋+水泥)→legendary(钢架+铱)
- 急单采购：无材料库存时自动 1.5 倍溢价
- 建筑: fence, well, root_cellar, tool_shed, barn, coop, irrigation

---

## 11. 工具系统

| 工具 | 作用 | 升级链 |
|------|------|--------|
| hoe | till 开垦 | copper→iron→steel→iridium |
| watering_can | 浇水（钢级溅射） | 同上 |
| sickle | 收获省体 20-40% | 同上 |
| spade | 排水省体 15-45% | 同上 |
| hammer | 建造加速 10-25% | 同上 |

---

## 12. 排水系统

- `drain` 每格 -30% 水分
- 暴雨/洪水后根部缺氧 → 必须排水
- spade 升级降低体力消耗

---

## 13. 牲畜系统

- chicken, cow, sheep, pig, bee
- 喂养/治疗/产品收集/屠宰/放牧/庇护/繁殖
- 粪便系统: manure_stockpile → spread_manure 施肥

---

## 14. 经济系统

- 累进税: <500G=0% / <5k=0.1% / <10k=0.5% / <20k=1% / >20k=1.5%
- 买卖/贷款/合同/保险
- 食品市场：独立价格，保质期系统

---

## 15. 感官感知（SenseCompiler）

**Phase E4**. 物理数据 → 确定性自然语言的编译层。

### 文件
- `sensory_dictionary.json` — 20 条映射规则
- `sense_compiler.py` — 372 行, 5 种匹配器

### 匹配器类型
- `range`: 数值范围匹配（土壤水分子0-10%→"干裂发白"）
- `boolean`: 布尔匹配（frost_damaged=true→"叶片萎蔫发黑")
- `equals`: 精确匹配（weather=="stormy"→暴风雨警报）
- `boolean_invert`: 反向匹配（fed_today=false→"还没喂"）

### 三级感知精度（Phase W4）

| 距离 | 精度 | 获得信息 |
|------|------|---------|
| 远距(5-15) | 轮廓 | biome类型、建筑存在 |
| 中距(2-5) | 识别 | 树种、动物踪迹、水源 |
| 近距(0-2) | 精确 | 土壤质地、木材质量、资源点 |

### 设计原则
- 确定性：相同物理状态 → 相同描述
- `perception_bias` 参数预留: 老农 bias=0.3（精准），新手 bias=1.2（模糊）
- 热重载: `SenseCompiler.reload_dictionary()`

---

## 16. 事件Schema与重要性评分

**Phase E5**. 统一 FarmEvent Schema v1.1 + 记忆管理。

### 文件
- `vault_utils.py` — 452 行, 57 种 event_type 枚举
- `importance_scorer.py` — 401 行, 13 条评分规则

### FarmEvent 结构
```json
{
  "event_type": "action_harvest",
  "importance_score": 7,
  "tags": ["harvest", "summer"],
  "context_before": {gold, energy, weather, hour, near_harvest_crops...},
  "outcome": {estimated_value, quality, revenue...},
  "timestamp": "ISO8601"
}
```

### 重要性评分规则（13条）
- 收获成熟作物: +3
- 收获 S/A 级品质: +2
- 收获价值>500G: +2
- 建新建筑: +3
- 重要失败: +2
- 治疗动物: +2
- 升级工具: +2
- 日常维护: +1
- 元操作(sleep/next_day): 0

### 记忆合成器
- `MemorySynthesizer.synthesize_day()`: 日终评分+过滤
- `MemorySynthesizer.generate_reflection()`: 季末洞察生成
- 自动写 `history/` 日合成 + `knowledge/learned/` 季反思

---

## 17. 紧急中断系统

**Phase E6**. 确定性中断检测——不替代 LLM 决策，而是标注紧急状况。

### 文件
- `interrupt_system.py` — 371 行, 10 个触发器

### 中断优先级

| 优先级 | 触发器 | 触发条件 |
|--------|--------|---------|
| P0 🔴 | energy_collapse | 体力<15, 非夜间 |
| P0 🔴 | mature_crops_about_to_rot | ≥3株成熟, 下午, 非夜间 |
| P1 🟠 | frost_threat_to_near_harvest | 霜冻预警 + 有接近成熟作物 |
| P1 🟠 | storm_with_vulnerable_crops | 暴风雨 + 有作物 |
| P1 🟠 | repeated_failure_loop | 同动作连续失败≥2次 |
| P2 🟡 | starvation_warning | 饥饿<15 |
| P2 🟡 | drought_with_unwatered_crops | 旱灾 + ≥3株未浇水 |
| P2 🟡 | sick_animal_untreated | 有生病动物 |
| P2 🟡 | storage_overflow_risk | 仓库 90%+ 满 + 有成熟作物 |
| P2 🟡 | extreme_sleepiness | 睡意≥70, 非夜间 |

### 特性
- 每触发器独立冷却周期
- 中断 header 注入 LLM prompt 顶部
- 日变更时自动重置冷却

---

## 18. 记忆系统（MemGPT三层）

- 工作记忆: `_working_memory`, 每 cycle 注入
- 短期记忆: `remember(topic, content)` → `knowledge/learned/{topic}.md`
- 长期记忆: 每 50 cycles `_memory_consolidate()` → LLM 压缩
- `recall(topic)`: 搜索 vault, 结果注入下一 cycle
- `forget(topic)`: 标记 [STALE]

---

## 19. 季节报告与跨年学习

- `_season_report()`: 季末从 JSONL 聚合决策数据
- `_inject_past_season_report()`: 新季节时注入去年同期报告
- 报告含: 成功率、经济活动、主要作物、天气统计

---

## 20. Agent身份与性格（三层模型）

**Phase W1**. 每个 Agent 拥有独特的、可演化的性格。

### 文件
- `agent_profile.py` — 283 行
- `agents/profiles/{id}.json` — 3 个预设: xu_renwu(农夫), old_wang(畜牧者), iron_lady(工匠)

### 三层性格模型

**1. 固定特质（6维，不可变）:**
- industriousness (勤奋), intelligence (聪明), social_aptitude (社交), curiosity (好奇心), patience (耐心), bravery (勇气)

**2. 习得偏好（11维，行为漂移）:**
- watering_enjoyment, harvesting_satisfaction, reading_love, social_desire, building_interest, exploration_urge, animal_affinity, crafting_drive, drainage_anxiety, risk_appetite_modifier, frugality_habit
- 每执行相关动作 +0.01 漂移

**3. 社会影响（暂态，衰减）:**
- recent_interactions（保留最近 10 次）
- 20+ cycles 无交互 → 显著衰减

### 性格→Prompt
`AgentProfile.personality_snippet()` 生成紧凑的 NL 性格描述注入 LLM system prompt。

### 命令行参数
```bash
python agent-world-llm.py --profile xu_renwu   # 农夫
python agent-world-llm.py --profile old_wang    # 畜牧者
python agent-world-llm.py --profile iron_lady   # 工匠
```

---

## 21. 技能树系统（三职业分支）

**Phase W1**. JSON 定义的技能树，分支路线。

### 文件
- `skill_tree.py` — 301 行
- `agents/skills/farmer_tree.json` — 10 节点
- `agents/skills/herder_tree.json` — 8 节点
- `agents/skills/craftsman_tree.json` — 8 节点

### 农夫技能树
```
基础耕作(Lv0)
  └→ 高效耕作(Lv1)
       ├→ 灌溉专精(Lv2) → 水利工程(Lv3) 或 旱作农业(Lv3)
       ├→ 土壤改良(Lv2) → 有机农业(Lv4)
       └→ 机械化(Lv2) → 畜力耕作(Lv4) → 蒸汽农机(Lv6)
```

### 畜牧者技能树
```
基础畜牧(Lv0)
  ├→ 放牧技巧(Lv1) → 游牧经济(Lv3)
  ├→ 兽医知识(Lv1) → 良种培育(Lv3) → 畜群管理(Lv4) → 动物低语者(Lv6)
  └→ 畜产品加工(Lv2)
```

### 工匠技能树
```
基础锻造(Lv0)
  ├→ 工具锻造(Lv1) → 精密铸造(Lv3) → 大师工艺(Lv5)
  ├→ 建筑工艺(Lv1) → 景观建筑(Lv3)
  ├→ 装饰工艺(Lv2)
  └→ 矿产勘探(Lv2)
```

### XP 机制
- 每动作按 `BASE_ACTION_XP` 表获取
- 角色加成: 农夫 farming×1.5, 畜牧者 husbandry×1.5, 工匠 construction×1.5
- 分支节点: 选了一条路线后，同级兄弟节点永久锁定

---

## 22. 个人知识图谱（KnowledgeMap）

**Phase W1**. 每个 Agent 对世界的私有、不完整认知。

### 文件
- `knowledge_map.py` — 336 行

### 数据结构

**TileKnowledge:**
```python
pos_x, pos_y, last_observed_cycle
biome (远距可见)
tree_species, animal_signs, water_nearby (中距)
soil_type, moisture_estimate, wood_quality, hidden_resource (近距)
```

**AgentPerception:**
- trust_level (0-1，影响传言可信度)
- known_skills, shared_facts

### 核心方法
- `observe_tile(x, y, distance, raw_data, cycle)`: 分三级填充
- `get_stale_tiles(cycle, threshold)`: 过期地块检测
- `add_fact(fact_id, desc, source)`: book/experience/social
- `has_fact(fact_id)`: 知识检查
- `tile_summary_for_prompt()`: NL 压缩

### 持久化
- `agents/knowledge/{agent_id}.json`

---

## 23. 多Agent与社交系统

**Phase W2**. 多个 Agent 共享同一个物理世界，通过社交交互。

### 文件
- `multi_agent_launcher.py` — 159 行
- `agent-world-llm.py --profile` 参数化

### 社交动作

**social_msg(target, message):**
- 写入 `PARENT_VAULT/social/{target}_inbox.md`
- 接收方下次 cycle 读取并清空
- 25% 概率附带传言传播

**social_lookup(target):**
- 读取 `agents/{target}/state/farm.md`
- 返回对方农场快照

### 多 Agent 启动
```bash
python multi_agent_launcher.py                    # 全部3个
python multi_agent_launcher.py --agents xu_renwu,old_wang
```

每个 Agent:
- 独立 sub-vault (`agents/{profile_id}/`)
- 独立农场 (每人注册唯一 farm)
- 共享 `PARENT_VAULT` 资源 (profiles, skill trees, social inbox)

### 社交感知注入
每个 Agent 的 prompt 中显示其他 Agent:
```
👥 你认识的人
🌾续仁武(farmer), 🐄老王(herder), 🔨铁娘子(craftsman)
```

---

## 24. 书籍系统

**Phase W3**. 10 本可读书籍，6 种类别，阅读进度+效果引擎。

### 文件
- `book_engine.py` — 370 行
- `agents/books/_catalog.json` — 210 行, 10 本

### 六类书籍

| 类别 | 图标 | 效果 | 例子 |
|------|------|------|------|
| 技能书 | 📘 | 技能 +XP | 《小麦种植指南》+40 farming |
| 科普书 | 🔬 | 解锁世界观知识 | 《牧场经济学》→草原放牧知识 |
| 哲学书 | 📜 | 性格漂移 | 《不确定性的礼物》+0.15风险承受 |
| 历史书 | 📚 | 注入跨年记忆 | 《三年大旱纪》→储粮知识 |
| 故事书 | 📖 | 偏好微调+恢复疲劳 | 《鲁滨逊漂流记》-15疲劳 |
| 日记 | 📝 | agent 自撰或他人记录 | agent 用 LLM 生成 |

### 阅读机制
- 每本书 2-5 章，每章 30 分钟
- `read_book(book_id)` 读一章，或 `read_book()` 自动选
- `buy_book(book_id)` 市场购买
- 识字要求: story 0.1, skill 0.2, science 0.4, philosophy 0.6
- 夜间需要 lamp
- 读完触发效果: skill_xp / unlocks_fact / preference_shift / fatigue_recovery

### Agent 撰写
- 高技能 agent 可 `write_book` 调用 LLM 生成日记
- 写入 shared catalog，其他 agent 可读

---

## 25. Biome地形系统

**Phase W4**. 50×50 世界，6 种真实地貌。

### 文件
- `biomes.json` — 142 行
- `agent_world_local.py` — 重写 `generate_terrain()`

### 地形生成流程
1. Diamond-square 高程生成（多 pass 平滑）
2. 4-10 个随机水源（pond/stream）
3. 基于 高程序+距水距离+坡度 分配 biome
4. 按 biome 参数生成: 土壤类型/NPK/有机质/pH/水分/表土
5. Microclimate: GDD 修正, 洪水风险, 霜冻修正
6. 稀有资源随机分布 (15%概率)

### 每个 Biome 的独特资源
- 冲积平原: fertile_silt, clay_deposit
- 草原: wild_herbs, medicinal_plants, calcic_layer
- 森林: mushroom, wild_berry, medicinal_herb, honey_tree
- 湿地: peat, reed, clay_deposit, fish
- 丘陵山地: iron_ore, copper_ore, stone, coal_seam, rare_earth
- 河岸: fish, clay_deposit, river_stone

---

## 26. 传言与错误信念

**Phase W5**. Agent 存在于信息不完整的世界——有些"知识"是错误的。

### 文件
- `rumor_engine.py` — 255 行, 12 个传言模板

### 传言来源

| 来源 | 可信度范围 | 准确性 |
|------|----------|--------|
| exploration (探索) | 0.5-0.8 | 大部分准确 |
| social (社交) | 0.4-0.7 | 一半准确 |
| overheard (道听途说) | 0.2-0.5 | 大部分不准确 |
| suspicion (直觉) | 0.3-0.5 | 基本不准确 |

### 传言生命周期
1. **生成:** 探索(30%) / 社交(25%) / 空闲反思(10%, 每30cycles)
2. **存储:** 写入 `KnowledgeMap.rumors`, 持久化到 `agents/knowledge/{id}.json`
3. **显示:** 未验证传言注入 Agent context
4. **验证:** 探索对应 tile → `verify_rumors_at()` 对比真相
5. **反馈:** ✅ 传言证实 / ❌ 传言破灭 → 注入 lookup_result

### 示例传言
- "山里的石头颜色很深，可能有铁矿" (50%准确)
- "北边山里有座金矿" (完全虚假)
- "森林深处据说生长着珍贵的药材" (60%准确)

---

## 27. 探索与地图迷雾

**Phase W4**. 世界不是一开始就知道的——Agent 通过探索逐步发现。

### explore 动作
```json
{"action": "explore", "positions": [[x, y]]}
```

### 返回数据
- biome 类型 + 中文显示名
- 土壤类型 / 水分 / NPK / 表土深度
- 隐藏资源（如有）
- 附近水源 (≤5 格)
- 距离估算描述（"就在附近"/"需要走一段"/"相当偏远"）

### KnowledgeMap 集成
- 探索结果自动写入 `KnowledgeMap.observe_tile()`
- 距离分级: ≤2=近距, ≤5=中距, >5=远距
- 同时触发传言验证
- 体力消耗 +5, 睡意 +3

---

## 28. 野生动物生态系统（Phase W6）

### 概述
13 种野生动物分布在 6 种 biome 中，形成完整的捕食食物链。种群数量、饱食度、繁殖/死亡每日更新。野生动物与农场交互，造成作物损失或牲畜攻击。

### 文件
- `ecology.json` — 13 物种定义（食性、捕食链、农场交互）
- `ecology_engine.py` — 种群动态引擎（~360 行）

### 13 物种概览

| 物种 | 食性 | 天敌 | 农场影响 |
|------|------|------|---------|
| 野兔 | 草食 | 狐狸/狼/鹰 | 偷吃未收作物 |
| 鹿 | 草食 | 狼 | 偷吃作物 + 踩踏幼苗 |
| 野猪 | 杂食 | 狼 | 拱地破坏翻耕地 |
| 狼 | 肉食 | — | **攻击牲畜** |
| 狐狸 | 杂食 | 狼/鹰 | 偷吃鸡蛋 |
| 鹰 | 肉食 | — | 抓小鸡 |
| 野羊 | 草食 | 狼 | 和家羊抢草场 |
| 猫头鹰 | 肉食 | — | 无害（吃鼠） |
| 蛙 | 食虫 | 鹭 | 有益（吃害虫） |
| 鹭 | 肉食 | — | 吃池塘鱼 |
| 鱼 | 草食 | 鹭 | 无害 |

### 狼攻击牲畜的二元触发

| 攻击类型 | 触发条件 | 描述 | Agent 反思 |
|---------|---------|------|-----------|
| 饥饿攻击 | satiation < 30 | "狼群因为饥饿袭击了羊圈——冬天的猎物太少了" | 生态失衡 |
| 机会攻击 | 围栏破损 | "一只狼从破损的围栏钻进了羊圈" | 自己的疏忽 |

### Agent 感知

SenseCompiler 对野生动物生成三级精度描述：

| 距离 | 示例描述 |
|------|---------|
| 远距 | "远处的山上传来了狼嚎" / "天空中有鹰在盘旋" |
| 中距 | "地上有大大的狼爪印——离农场不远了" |
| 近距 | "羊圈外面有狼的新鲜足迹！昨晚来过了！" |

### 生态中断（2 个新增）

| 中断 | 优先级 | 触发 |
|------|--------|------|
| wolf_attack | P1 | ecology_alerts 包含 livestock_attack |
| crop_raided | P2 | 作物大面积被破坏 |

### API 新增字段
- `ecology_observations`: 近距生态感知文本列表
- `ecology_distant`: 远距生态感知
- `wolf_warning`: 狼群威胁评估
- `ecology_alerts`: 当日生态事件

---

## 29. 动作完整清单（60+）

### 农耕 (11)
`till, till_bulk, plant, plant_bulk, plant_tree, water, harvest, weed_all, save_seeds, prune, fell_tree`

### 土壤管理 (8)
`fertilize, compost, apply_compost, green_manure, mulch, lime, sulfur, spread_manure`

### 排水灌溉 (4)
`drain, irrigate_drip, irrigate_flood, irrigate_sprinkler`

### 经济 (9)
`buy, sell_storage, buy_animal, buy_tool, buy_material, loan, repay_loan, sign_contract, deliver_contract, cancel_insurance`

### 建造 (3)
`build, propose_building, bury`

### 工具 (2)
`forge, repair`

### 牲畜 (9)
`feed_animals, water_animals, collect_products, treat_animal, breed, slaughter, send_to_pasture, bring_to_shelter, isolate`

### 身体 (7)
`eat, drink_water, drink_coffee, sleep, exercise, read, research`

### 加工 (2)
`process, process_food`

### 认知 (7)
`remember, recall, forget, lookup, read_book, buy_book, explore`

### 社交 (3)
`social_msg, social_lookup, move`

### 时间 (1)
`next_day`

---

## 30. 文件架构

```
C:\agent-brain\                          ← 生产环境
├── agent_world_local.py   (4500+行)     ← 物理引擎服务器
├── agent-world-llm.py     (1550+行)     ← LLM Agent 决策大脑
├── vault_utils.py          (452行)      ← 共享 I/O + FarmEvent Schema
├── sense_compiler.py       (372行)      ← 感官编译器 (E4)
├── sensory_dictionary.json (178行)      ← 感官映射规则 (E4)
├── importance_scorer.py    (401行)      ← 重要性评分+记忆合成 (E5)
├── interrupt_system.py     (371行)      ← 紧急中断 (E6)
├── agent_profile.py        (283行)      ← Agent身份+性格 (W1)
├── skill_tree.py           (301行)      ← 技能树引擎 (W1)
├── knowledge_map.py        (390行)      ← 个人知识图谱 (W1+W5)
├── book_engine.py          (370行)      ← 书籍引擎 (W3)
├── rumor_engine.py         (255行)      ← 传言引擎 (W5)
├── multi_agent_launcher.py (159行)      ← 多Agent启动器 (W2)
├── biomes.json             (142行)      ← Biome定义 (W4)
├── launch_llm.py                        ← 单Agent启动
├── .env                                 ← API key
│
├── agents/                              ← 多Agent存储
│   ├── profiles/         (3 JSON)       ← Agent预设身份
│   ├── skills/           (3 JSON)       ← 技能树定义
│   ├── books/_catalog.json              ← 书籍目录
│   ├── knowledge/                        ← 知识图谱存储
│   └── {agent_id}/                      ← 每Agent独立sub-vault
│       ├── knowledge/   learned/ reports/
│       ├── decisions/   decision_log.jsonl
│       ├── history/     synthesis + archives
│       ├── state/       farm.md
│       └── memory/      plans.md
│
├── social/                              ← 跨Agent社交收件箱
├── tests/                               ← 测试脚本
├── history/                             ← 归档
├── knowledge/                           ← 共享知识库
├── state/      farm.md                  ← 农场快照
└── decisions/  Y1/ Y2/                  ← 决策日志 (3312条)
```

---

## 31. API参考

### Agent World (8080)
```
POST /api/agents/register  → 注册Agent
POST /api/agents/verify    → 数学验证
```

### NeverLand Farm (8081)
```
GET  /api/game/config                          → 作物配置
GET  /api/farm/{id}/status                     → 农场完整状态
POST /api/farm/{id}/action                     → 执行动作 (57种)
POST /api/farm/{id}/next-day                   → 进入下一天
POST /api/farm/register                        → 注册新农场
GET  /api/farms                                → 所有农场列表
GET  /api/market/prices                        → 市场价格
```

### AfterGateway Bar (8082)
```
POST /api/v1/drink/random                      → 随机饮品
POST /api/v1/guestbook/entries                 → 留言板
```

### 状态返回新增字段（Phase W4+）
- `terrain_biome`: 50×50 biome 网格
- `terrain_elevation`: 50×50 高程网格
- `biome_resources`: 50×50 资源网格
- `sensory_observations`: NL 感官报告（SenseCompiler 编译）
- `hour`: 当前时钟时间 (24h 连续)
- `is_night`: 是否夜间

---

> 版本: Phase W5 | 2026-06-18 | 续仁武 | 12 phases · 60+ 文件 · 8500+ 行
