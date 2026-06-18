# Agent World 农场世界 — 完整设定参考

> 版本: Phase D4.1 | 2026-06-17 | 续仁武

---

## 目录

1. [世界架构](#1-世界架构)
2. [24小时时间系统](#2-24小时时间系统)
3. [天气与温度](#3-天气与温度)
4. [土壤与地形](#4-土壤与地形)
5. [作物系统](#5-作物系统)
6. [品质与遗传](#6-品质与遗传)
7. [农夫身体](#7-农夫身体)
8. [睡意系统](#8-睡意系统)
9. [体质与健身](#9-体质与健身)
10. [学识与阅读](#10-学识与阅读)
11. [建造系统](#11-建造系统)
12. [材料系统](#12-材料系统)
13. [牲畜系统](#13-牲畜系统)
14. [经济系统](#14-经济系统)
15. [孟德尔遗传](#15-孟德尔遗传)
16. [Agent 记忆系统](#16-agent-记忆系统)
17. [动作完整清单](#17-动作完整清单)
18. [API 参考](#18-api-参考)

---

## 1. 世界架构

### 三服务器架构

| 端口 | 服务 | 功能 |
|------|------|------|
| 8080 | World | 智能体注册、认证、身份管理 |
| 8081 | NeverLand Farm | 农场核心——翻耕、种植、浇水、收获、建造、动物 |
| 8082 | AfterGateway Bar | 虚拟酒吧——饮品、留言簿 |

### 农场地图

- **20×28 格** 网格
- 海拔：0-10 米（影响微气候和温度）
- 土壤类型：沙土（sand）、壤土（loam）、黏土（clay）
- 区域：农田（farmland）、果园（orchard）、水源（water_sources）
- 水源：池塘（pond）、溪流（stream）——影响灌溉距离衰减
- 微气候：每格独立 GDD 修正值（朝向、海拔、湿度）

### 游戏时间

- 每年 4 季 × 28 天 = 112 天
- 季节顺序：Spring → Summer → Fall → Winter
- 农场注册时从 Day 1 开始（随机季节）

---

## 2. 24小时时间系统

**Phase D1：替代旧日相系统**

### 季节光照时长

| 季节 | 日照时间 | 说明 |
|------|---------|------|
| Spring | 14h | 温和日长 |
| Summer | **16h** | 最长白天 |
| Fall | 11h | 日短夜长 |
| Winter | **9h** | 最短白天 |

### 时间消耗（每动作）

| 动作 | 耗时 | 动作 | 耗时 |
|------|------|------|------|
| till（开垦） | 1.5h | till_bulk（批量） | 1.0h/格 |
| plant（种植） | 0.8h | plant_bulk（批量） | 0.5h/格 |
| water（浇水） | 0.4h | harvest（收获） | 0.6h |
| build（建造） | 2.0h | buy/sell | 0.3h |
| eat | 0.3h | drink_water | 0.1h |
| sleep | 0h | exercise | 0.8h |
| read | 1.0h | research | 1.5h |
| feed_animals | 按物种 | 见牲畜节 |
| buy_material | 0.3h | propose_building | 1.0h |

### 夜间限制（剩余 ≤ 2h）

以下活动**夜间禁止**：
till, plant, harvest, build, fertilize, green_manure, spread_manure, lime, sulfur, irrigate_flood, plant_tree, fell_tree, slaughter, bury

夜间**允许**：read, exercise, sleep, eat, drink_water, research, propose_building

### 批次折扣

批量开垦/种植时，首块按标准时间，后续每块按折扣时间计算：
- till_bulk: 首格 1.5h + 余格 × 1.0h
- plant_bulk: 首格 0.8h + 余格 × 0.5h

体质 ≥ 2.0 时，整体时间 × 0.8 加速。

---

## 3. 天气与温度

### 天气状态（8种）

sunny, cloudy, rainy, stormy, frost, drought, heat_wave, flood

### 马尔可夫转移（每小时）

天气通过 8×8 转移矩阵按小时切换，**带季节修正**：

| 季节 | 修正 |
|------|------|
| Spring 02:00-07:00 | 霜冻概率 ×1.5 |
| Summer 12:00-17:00 | 热浪概率 ×1.3 |
| Fall 全天 | 雨/暴风雨/洪水概率 ×1.25 |
| Winter 20:00-09:00 | 霜冻概率 ×2.0 |

### 温度计算

```
温度 = 季节基线(day1→day28插值) + 日变化曲线(小时) + 天气修正 + 海拔修正
```

- 海拔修正: (海拔 - 5) × -0.6°C（每升高 1 米降 0.6°C）
- Spring: Day1=6°C → Day28=18°C
- Summer: Day1=22°C → Day28=24°C
- Fall: Day1=18°C → Day28=4°C
- Winter: Day1=4°C → Day28=1°C

### 霜冻系统

| 等级 | 触发 | 效果 |
|------|------|------|
| 🌱 轻度 | 温度 ≤ Tbase×0.7, stage=0 | 5% 霜冻伤害 |
| 🥀 中度 | 温度 ≤ Tbase×0.5, stage≤1 | 15% 幼苗死亡 |
| ☠ 重度 | 温度 ≤ Tbase×0.3 | 50% 幼苗死亡，任何作物受损 |

冻伤的作物当天 GDD 归零。

### 天气对 GDD 的影响

| 天气 | GDD 倍率 |
|------|---------|
| sunny | ×1.0 |
| cloudy | ×0.8 |
| rainy | ×0.5 |
| stormy | ×0.2 |
| drought | ×0.6 |
| frost | ×0.1 |
| flood | ×0.0 |

---

## 4. 土壤与地形

### 土壤类型

| 类型 | 初始表土深度 | 保水性 | 排水 | 耕作难度 | 养分保持 |
|------|------------|--------|------|---------|---------|
| sand（沙土） | 18-30cm | 0.3 | 0.8 | 1.2 | 0.4 |
| loam（壤土） | 20-28cm | 0.6 | 0.5 | 1.0 | 0.7 |
| clay（黏土） | 15-25cm | 0.9 | 0.2 | 0.7 | 1.0 |

### 表土侵蚀

裸露翻耕地在 **rainy/stormy/flood** 天气每小时流失表土：

| 天气 | 侵蚀速率 |
|------|---------|
| rain | 0.03cm/h |
| stormy | 0.08cm/h |
| flood | 0.15cm/h |

干土流失更快（干燥因子 > 1.0）。**作物覆盖完全阻止侵蚀。**

| 表土深度 | GDD 修正 | 品质上限 |
|---------|---------|---------|
| < 5cm | ×0.5 | 最高 B 级 |
| < 8cm | ×0.75 | 最高 A 级 |
| ≥ 8cm | 正常 | 正常 |

### NPK + 有机质 + pH

每格独立：N(0-80), P(0-80), K(0-80), 有机质(0-25), pH(4.0-8.0)

### pH 钟形修正

```
偏离 ≤ tol×0.5 → GDD×1.0（理想）
偏离 ≤ tol → GDD×0.85（可接受）
偏离 ≤ tol×1.5 → GDD×0.6（显著抑制）
偏离 > tol×1.5 → GDD×0.3（严重毒性）
```

### 有机质恢复

| 方法 | OM 增量 | 其他效果 |
|------|--------|---------|
| fertilize（化肥） | 0 | +15N +10P +10K（不加 OM） |
| green_manure（绿肥） | +20 | +10N |
| compost → apply_compost | +15 | +8N +5P +5K |
| spread_manure（施粪） | +5/格 | +5N +3P +5K |
| weed_all（除草） | +2-3/格 | 清除杂草 |

### 杂草

- 空闲翻耕地每天 12% 概率生长（春季 ×1.5，冬季 ×0.2）
- 杂草邻域掠夺相邻作物养分（每株 -3% NPK）
- weed_all: 1.5h, 体力 -30, 每格 +2-3 有机质

---

## 5. 作物系统

### 全部 14 种作物

```python
# Spring（14h 光照）
parsnip:    GDD=24, buy=20, sell=50,  root,     水
potato:     GDD=36, buy=40, sell=100, root,     水
strawberry: GDD=36, buy=80, sell=150, berry, 多收, 虫媒
cauliflower:GDD=48, buy=60, sell=200, brassica, 虫媒
tulip:      GDD=36, buy=25, sell=80,  flower,   虫媒

# Summer（16h 光照）
wheat:   GDD=30, buy=25, sell=70,  grain, 产秸秆
soybean: GDD=42, buy=30, sell=90,  legume,固氮+N
tomato:  GDD=54, buy=50, sell=130, vine,  多收,虫媒,热敏×2.5
corn:    GDD=60, buy=100,sell=150, grain, 产秸秆
melon:   GDD=90, buy=60, sell=140, vine,  虫媒
blueberry:GDD=90,buy=60, sell=100, berry, 多收,虫媒,热敏×2.0, 需酸性pH5.0

# Fall（11h 光照）
wheat:   GDD=30, buy=25, sell=70,  grain
pumpkin: GDD=50, buy=100,sell=320, vine,  虫媒（利润之王）
corn:    GDD=60, buy=100,sell=150, grain

# Winter（9h 光照）
winter_seeds: GDD=15, buy=56, sell=80, root
powder_melon: GDD=40, buy=70, sell=180,vine（全季可种）
```

### GDD 累积公式

```
每日 GDD = SEASON_BASE × WEATHER_GDD[天气] × 微气候修正 × pH修正 × 表土封顶 × 学识加成
```

季节基数：Spring=12, Summer=18, Fall=10, Winter=5

### 生长阶段

每作物 6 阶段（0-5），从 GDD 比率自动卡位。

---

## 6. 品质与遗传

### 品质等级

| 等级 | 售价倍率 | 保鲜修正 | 留种遗传加成 |
|------|---------|---------|------------|
| S | ×2.0 | +50% | +0.15 |
| A | ×1.5 | +20% | +0.05 |
| B | ×1.0 | 标准 | 0 |
| C | ×0.5 | -30% | -0.1 |

### 品质概率分布（按 qm 值）

| qm 范围 | P(S) | P(A) | P(B) | P(C) |
|---------|------|------|------|------|
| ≥1.6 | 25% | 45% | 25% | 5% |
| 1.3~1.6 | 10% | 40% | 40% | 10% |
| 1.0~1.3 | 3% | 25% | 55% | 17% |
| 0.7~1.0 | 0% | 8% | 50% | 42% |
| <0.7 | 0% | 0% | 20% | 80% |

### qm 构成链

```
qm = 1.0 + genetic_bonus（种子遗传加成）
× 连作惩罚: 2次×0.85, 3+次×0.6
× 轮作奖励: 不同科 ×1.15
× 缺水惩罚: ×0.85 / ×0.7 / ×0.5
× 风暴伤害: ×0.7
× pH偏离: ×0.3-1.0
× 表土惩罚: <5cm×0.7
× 有机质惩罚: <3×0.6
× 传粉不足: ×0.3-1.0（仅虫媒）
× prune_bonus: ×1.3-1.4（多年生修剪）
```

品质上限封顶：qm=0.2 ~ 2.0

### 土壤健康封顶（覆盖概率结果）

| 条件 | 最高品质 |
|------|---------|
| 有机质 < 3 | C 级锁定 |
| 有机质 < 5 | 最高 B |
| pH 偏离 > tol×1.5 | 最高 B |
| pH 偏离 > tol×1.2 | 最高 A |
| 表土 < 5cm | 最高 B |
| 表土 < 8cm | 最高 A |

---

## 7. 农夫身体

### 基础属性

| 属性 | 范围 | 自然衰减 | 说明 |
|------|------|---------|------|
| ⚡ 体力 | 0-200 | 按动作消耗 | 0 = 无法行动 |
| 🍖 饥饿 | 0-100 | -12/天 | <20=能耗×1.5 |
| 💧 口渴 | 0-100 | -10/天 | <20=能耗×1.3 |
| 😴 疲劳 | 0-100 | -30/晚恢复 | >80=能耗×1.5 |
| 💪 体质 | 1.0-3.0 | -0.05/天→1.0 | 越高越省力省时 |
| 📖 学识 | 0-5/主题 | 缓慢衰减 | 被动加成 |
| 😴 睡意 | 0-80 | +2.5/h白天 +7/h夜晚 | ≥80=强制睡觉 |

### 四大技能

| 技能 | 主要 XP 来源 | 升级阈值 |
|------|-------------|---------|
| 🌾 farming（农耕） | 种/收/浇水/施肥/除草/开垦 | 100/250/500/1000 XP |
| 🐄 husbandry（畜牧） | 喂食/饮水/收集/屠宰/配种 | 同上 |
| 🔧 machinery（机械） | 建造/修理/锻造 | 同上 |
| 🔬 processing（加工） | 加工/堆肥 | 同上 |

---

## 8. 睡意系统

### Phase D4: 睡意数值体系

| 机制 | 数值 |
|------|------|
| 最大睡意 | 80 |
| 白天清醒累积 | +2.5/h |
| 夜间清醒累积 | +7.0/h（比白天快 2.8 倍） |
| 睡眠恢复 | -10/h（体质 2.0→-15/h，体质 3.0→-20/h） |
| 强制睡眠 | ≥80（其他动作全部封禁） |
| 失误概率（低） | >60 → 2%/次 |
| 失误概率（高） | >70 → 5%/次 |

### 熬夜惩罚

跨日边界时睡意 >40 → 睡意 ×1.3（通宵代价滚雪球）
疲劳 >60 → 睡意 +5（疲劳耦合——越累越困）

### ☕ 咖啡

25G, -15 睡意, +10 疲劳, 耗时 0.2h。白天应急提神——代价是过后更累。

### 9 种失误文本

随机抽取："手抖了"、"看花了眼"、"迷迷糊糊"、"差点睡着"等——让睡意高的代价真实可感。

---

## 9. 体质与健身

### 体质数值

- 范围：1.0-3.0
- 通过 `exercise` 提升（+0.01~0.05/次）
- 衰减：0.05/天趋向 1.0
- 收益递减：越接近 3.0，增加越慢

### 体质效果

| 体质值 | 体力节省 | 时间加速 | 睡眠恢复加成 |
|--------|---------|---------|------------|
| 1.0 | 基准 | ×1.0 | +0 |
| 1.5 | ~10% | ×0.9 | +5/小时 |
| 2.0 | ~20% | ×0.8 | +10/小时 |
| 3.0 | ~40% | ×0.6 | +20/小时 |

适用范围：翻耕/种植/浇水/收获/除草/建造/伐木等**所有体力劳动**。

---

## 10. 学识与阅读

### 四大学识主题

| 主题 | 被动效果（Lv2+） |
|------|-----------------|
| 📖 farming | 每级 +3% GDD（Lv2=+3%, Lv5=+12%） |
| 📖 husbandry | 每级 +5% 动物产品售价（Lv5=+20%） |
| 📖 machinery | 每级 -5% 建造工期（Lv5=-20%） |
| 📖 economics | Lv2+ 解锁价格洞察 |

### 阅读动作

- `read/topic`: +0.05~0.08 / 次, 范围 0-5.0
- 夜间安全（可以在黑暗中阅读）
- 消耗 1.0h, 轻度疲劳 +3

---

## 11. 建造系统

### Phase D3: 材料制建造

```
建造流程: buy_material → 等待到货(3天) → build 消耗库存材料
```

### 14 栋建筑

| 建筑 | 材料等级 | 工期 | 效果 |
|------|---------|------|------|
| fence 围栏 | basic | 1d | 全灾害 -40% |
| well 水井 | basic | 7d | 灌溉 + 旱灾 -50% |
| coop 鸡舍 | standard | 2d | 容量4只鸡, 鸡蛋15G/天 |
| beehive 蜂箱 | standard | 3d | 容量3箱, 蜂蜜+传粉 |
| root_cellar 根窖 | standard | 3d | 仓库+100, 腐烂×0.4 |
| tool_shed 工具房 | quality | 3d | 全部体力 -30% |
| barn 畜棚 | quality | 4d | 容量4头牛羊猪 |
| mill 磨坊 | quality | 3d | 小麦→面粉增值1.5× |
| oil_press 榨油机 | quality | 3d | 大豆→豆油增值1.6× |
| silo 粮仓 | quality | 4d | 仓库+200 |
| smokehouse 熏制房 | quality | 3d | 肉→熏肉保值2× |
| cheese_room 奶酪间 | premium | 4d | 牛奶→奶酪保值3× |
| greenhouse 温室 | premium | 5d | 跨季种植任意作物 |
| sprinkler 喷灌 | premium | 14d | 自动浇水半径3格 |
| drip 滴灌 | legendary | 28d | 精准灌溉 GDD+5% |

### 建造天气影响

| 天气 | 当日进度 |
|------|---------|
| sunny/cloudy | 100% |
| rainy/stormy | 50% |
| frost/flood | 0%（停工） |

### 季节影响

Spring×0.9, Summer×1.2, Fall×1.0, Winter×1.5

### 每日体力消耗

轻量建筑 10/天, 中型 15/天, 重型 20/天。
体力不足→当日进度 0。

---

## 12. 材料系统

### 五级材料

| 等级 | 名称 | 价格倍率 | 寿命 | 解锁条件 |
|------|------|---------|------|---------|
| 🟤 basic | 原木+黏土 | ×1.0 | 5年 | 默认 |
| ⚪ standard | 木材+砖 | ×1.5 | 10年 | 机械Lv2+工具房 |
| 🔵 quality | 硬木+石料 | ×2.5 | 20年 | 机械Lv3 |
| 🟣 premium | 铁筋+水泥 | ×4.0 | 40年 | 机械Lv4+锻造 |
| 🟡 legendary | 钢架+铱合金 | ×8.0 | 100年 | 机械Lv5+铱矿+学识≥3 |

### 8 种可购买材料

| 材料 | 单价 | 等级 |
|------|------|------|
| wood_planks（木板） | 50G | standard |
| bricks（砖块） | 80G | standard |
| hardwood（硬木） | 150G | quality |
| stone（石料） | 120G | quality |
| iron_rebar（铁筋） | 300G | premium |
| cement（水泥） | 200G | premium |
| steel_frame（钢架） | 800G | legendary |
| iridium_alloy（铱合金） | 2000G | legendary |

材料采购后有 3 天到货延迟——需提前规划。

---

## 13. 牲畜系统

### 5 种动物

| 动物 | 买价 | 前置建筑 | 产品 | 产品价 | 寿命 | 回本天数 |
|------|------|---------|------|--------|------|---------|
| 🐔 鸡 | 500G | 鸡舍(1500G) | 鸡蛋 | 15G/天 | 56天 | **33天** |
| 🐑 羊 | 1,500G | 畜棚(4000G) | 羊毛 | 80G/3天 | 70天 | 56天 |
| 🐄 牛 | 2,500G | 畜棚(4000G) | 牛奶 | 35G/天 | 84天 | 71天 |
| 🐖 猪 | 1,800G | 畜棚(4000G) | 猪肉 | 600G/28天 | 56天 | 84天 |
| 🐝 蜜蜂 | 800G | 蜂箱(2000G) | 蜂蜜 | 50G/3天 | — | 48天 |

### 分种喂食时间（Phase D2）

| 动物 | 喂食 | 饮水 | 收集 |
|------|------|------|------|
| 鸡 | 0.08h | 0.05h | 0.15h |
| 羊 | 0.12h | 0.08h | 0.4h |
| 牛 | 0.15h | 0.10h | 0.5h |
| 猪 | 0.12h | 0.08h | 0.3h |
| 蜜蜂 | 0.02h | 0.02h | 0.2h |

### 疾病系统

- 基础患病率 0.5%/天
- 密度修正：>80% 容量时急速上升
- 夏季 ×1.5, 无通风 ×1.3
- 同建筑内传染：病畜 → 健康 15% × 密度修正
- 可隔离（isolate）、治疗（treat_animal）、通风升级（ventilate）

---

## 14. 经济系统

### 累进税制

| 金币区间 | 税率 |
|---------|------|
| <500G | **0%（免税）** |
| 500-5,000G | 0.1% |
| 5,000-10,000G | 0.5% |
| 10,000-20,000G | 1% |
| >20,000G | 1.5% |

### 起始资源

- 金币: 2,000G
- 种子: 0（必须先购买）
- 工具: 铜锄头 ×1, 铜水壶 ×1（耐久 60）

### 温度相关腐烂

```
腐烂速度 = 1.0 × (温度/20°C) × 作物热敏度
```

| 作物 | 热敏度 |
|------|--------|
| 草莓 | **3.0** |
| 番茄 | **2.5** |
| 蓝莓 | **2.0** |
| 防风草 | 1.5 |
| 小麦 | 1.0 |

夏季（23°C）草莓每天腐烂 3.45 点, 冬季（3°C）仅 0.45 点。
根窖 ×0.4, 粮仓对谷物 ×0.25。

### 合同系统

- 每季 Day1-3 发布 2-4 个合同
- 锁定价 +20% 高于现货
- 签约量越多 → 合同价下跌
- 违约罚金 30%

---

## 15. 孟德尔遗传

### 8 种可遗传性状

| 性状 | 效果 |
|------|------|
| 速生 | GDD 需求 -15% |
| 高产 | 产量 +20% |
| 耐寒 | 霜冻伤害 -50% |
| 耐热 | 热浪影响 -50% |
| 节水 | 需水量 -30% |
| 巨型 | 售价 +30%, GDD +25% |
| 珍品色 | 品质 qm +0.15 |
| 抗病 | 疾病概率 -60% |

### 遗传规则

- 庞纳特方格遗传
- 70% 显性, 30% 隐性
- 父母性状不同：25% 纯合各, 50% 杂合

### 发现机制

- 成熟动植物 2% 概率自然发现
- `research/topic=breeding` 主动分析
- 后代通过 `breed()` 从父母遗传性状

---

## 16. Agent 记忆系统

### MemGPT 三层架构

```
每周期注入 LLM 上下文：
🧠 最近 3 步 (JSONL) ← 始终可见
📝 工作记忆 ← Agent 自己写入，跨周期持久
🔍 召回结果 ← 上一次 recall() 的返回值
📖 自动回忆 ← 关键词匹配
📋 计划 ← 最后 6 行 plans.md
```

### Agent 自主记忆动作

| 动作 | 说明 |
|------|------|
| `remember(topic, content)` | 写入 `memory/knowledge/{topic}.md` |
| `recall(topic)` | 搜索 vault, 结果下周期注入 |
| `forget(topic)` | 标记 [STALE] |

### 每 50 周期整合

LLM 总结最近 20 步 → 压缩为一行工作记忆（等同于 MemGPT 的 summarize_messages_inplace）

### Vault 结构

```
decisions/Y{year}/{season}/day{NN}.md  ← 每天所有决策
knowledge/
  crops.md    strategy.md   soil.md
  body.md     buildings.md  farm-economy.md
state/farm.md  ← 全面实时快照
memory/
  reflections/  ← 季节反思
  plans.md      ← 行动轨迹
  knowledge/    ← Agent 自主写入的记忆
```

---

## 17. 动作完整清单

### 农耕动作

`till`, `till_bulk`, `plant`, `plant_bulk`, `water`, `harvest`, `fertilize`, `green_manure`, `compost`, `apply_compost`, `spread_manure`, `weed_all`, `lime`, `sulfur`

### 畜牧动作

`buy_animal`, `feed_animals`, `water_animals`, `collect_products`, `slaughter`, `breed`, `treat_animal`, `isolate`, `bury`, `ventilate`, `send_to_pasture`, `bring_to_shelter`

### 建造动作

`build`, `buy_material`, `propose_building`, `repair`, `forge`

### 经济动作

`buy`, `sell_storage`, `save_seeds`, `sign_contract`, `deliver_contract`, `process`

### 身体动作

`sleep`, `eat`, `drink_water`, `drink_coffee`, `exercise`, `read`

### 灌溉动作

`irrigate_flood`, `irrigate_sprinkler`, `irrigate_drip`

### 研究与记忆

`research`, `remember`, `recall`, `forget`, `lookup`

### 系统动作

`next_day`, `bar_drink`, `guestbook`

---

## 18. API 参考

### 关键端点

```
GET  /api/farm/{id}/status     — 农场完整状态
POST /api/farm/{id}/action     — 执行动作
POST /api/farm/{id}/next-day   — 推进到下一天
GET  /api/game/config           — 作物/游戏配置
GET  /api/market/prices         — 市场价格
GET  /api/market/contracts      — 当前合同
POST /api/agents/register       — 注册 agent
POST /api/agents/verify         — 验证码验证
```

### 状态响应包含

`season, day, year, weather, gold, score, energy, crops[], inventory_items[], land_status, farmer{}, buildings[], storage[], storage_capacity, day_hours_remaining, season_daylight, is_night, weed_count, avg_topsoil, weather_notes, frost_warning, available_contracts, signed_contracts, irrigation_status, livestock[], manure_stockpile, perennial_crops[], intercrop_tiles[]`

---

*文档生成于 2026-06-17 | 代码版本: Phase D4.1 | Agent World 本地模拟器 v6.0*
