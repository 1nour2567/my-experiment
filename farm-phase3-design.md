# Phase 3：动物 + 体力 + 技能 设计文档

> 2026-06-15

---

## 5. 动物与牲畜系统

### 5.1 数据模型

```python
ANIMALS = {
    "chicken": {"name":"鸡","buy":500,"sell":800,"feed_type":"grain","product":"egg",
        "product_interval":1,"product_price":15,"product_name":"鸡蛋",
        "feed_per_day":1,"water_per_day":0.5,
        "shelter":"coop","shelter_size":4,"gestation":0,"maturity_days":5,
        "lifespan":56,"manure_per_day":0.3,"space_per_head":1},
    "sheep":   {"name":"羊","buy":1500,"sell":2000,"feed_type":"hay","product":"wool",
        "product_interval":3,"product_price":80,"product_name":"羊毛",
        "feed_per_day":2,"water_per_day":1,
        "shelter":"barn","shelter_size":4,"gestation":21,"maturity_days":14,
        "lifespan":70,"manure_per_day":1.0,"space_per_head":2},
    "cow":     {"name":"牛","buy":2500,"sell":3500,"feed_type":"hay","product":"milk",
        "product_interval":1,"product_price":35,"product_name":"牛奶",
        "feed_per_day":3,"water_per_day":2,
        "shelter":"barn","shelter_size":4,"gestation":28,"maturity_days":21,
        "lifespan":84,"manure_per_day":2.0,"space_per_head":3},
    "pig":     {"name":"猪","buy":1800,"sell":2800,"feed_type":"grain","product":"meat",
        "product_interval":28,"product_price":600,"product_name":"猪肉",
        "feed_per_day":2.5,"water_per_day":1.5,
        "shelter":"barn","shelter_size":4,"gestation":21,"maturity_days":18,
        "lifespan":56,"manure_per_day":1.5,"space_per_head":2},
    "bee":     {"name":"蜜蜂","buy":800,"sell":800,"feed_type":"nectar","product":"honey",
        "product_interval":3,"product_price":50,"product_name":"蜂蜜",
        "feed_per_day":0,"water_per_day":0.2,
        "shelter":"beehive","shelter_size":3,"gestation":0,"maturity_days":7,
        "lifespan":365,"manure_per_day":0,"space_per_head":1,
        "pollination_boost":0.10},  # nearby crops get +10% GDD
}
```

### 5.2 农场中的动物实例

```python
f["livestock"] = [{
    "id": uuid, "species": "chicken", "name": "小黄",
    "age": 0, "health": 100, "happiness": 80,
    "fed_today": False, "watered_today": False,
    "in_shelter": True, "pasture_x": None, "pasture_y": None,
    "pregnant": False, "gestation_days": 0,
    "product_ready": False, "last_product_day": 0,
    "mother_id": None, "father_id": None,
}]
```

### 5.3 新增建筑

```python
BUILDINGS["coop"]   = {"name":"鸡舍","cost":1500,"desc":"养鸡专用，容量4只，日产蛋","effect":"chicken_house","build_days":2}
BUILDINGS["barn"]   = {"name":"畜棚","cost":4000,"desc":"养牛羊猪，容量4头","effect":"livestock_house","build_days":4}
BUILDINGS["beehive"]={"name":"蜂箱","cost":2000,"desc":"养蜂3箱，产蜜+作物授粉+10%GDD","effect":"bee_house","build_days":3}
```

### 5.4 新增动作

```python
"buy_animal":   {"species":"chicken","name":"小花"}     # 购买动物
"feed_animals": {}                                      # 喂所有动物
"water_animals": {}                                     # 给所有动物饮水
"collect_products": {}                                  # 收蛋/奶/毛/蜜
"send_to_pasture": {"animal_ids":[...]}                 # 放牧（节省饲料）
"bring_to_shelter": {"animal_ids":[...]}                # 圈回棚里
"slaughter":     {"animal_id":"..."}                     # 屠宰（猪→肉，老动物→少量肉）
"breed":         {"male_id":"...","female_id":"..."}     # 配种
```

### 5.5 每日动物循环（next-day 中）

1. **饥饿检查**：未喂 → health-15, happiness-20
2. **口渴检查**：未饮 → health-10
3. **无庇护+恶劣天气**：health-25
4. **放牧收益**：pasture 中 → feed 消耗减半，happiness+5
5. **产品生产**：health≥60 且 fed+watered → product_ready
6. **年龄增长**：age+1。超过 lifespan → 自然死亡
7. **怀孕推进**：gestation_days-1 → 归零时产仔1-2只
8. **粪便产出**：manure_per_day → 累积到 manure_stockpile
9. **草地消耗**：pasture 地块 grass-1。自然恢复 +0.5/天

### 5.6 粪肥系统

```python
f["manure_stockpile"] = 0  # 粪肥库存

# 撒粪肥
"spread_manure": {"positions":[[0,0],[1,0]]}
# 效果：+10有机质, +5N, +3P, +5K 每格
# 过度施用（单格 >3次/季）→ N 过量 → 烧苗 risk
```

### 5.7 蜜蜂授粉

有蜂箱 + 蜜蜂时，蜂箱周围 5 格曼哈顿距离内的作物：GDD 额外 +10%（pollination_boost）。这是被动效果，不需要 Agent 操作。

---

## 6. 体力系统

### 6.1 体力模型

```python
f["farmer"] = {
    "energy": {"current": 200, "max": 200},
    "hunger": {"current": 100, "max": 100},  # 0=饿死
    "hydration": {"current": 100, "max": 100},  # 0=脱水
    "fatigue": {"current": 0, "max": 100},  # 100=必须睡
    "skills": {"farming": 1, "husbandry": 1, "machinery": 1, "processing": 1},
    "xp": {"farming": 0, "husbandry": 0, "machinery": 0, "processing": 0},
}
```

### 6.2 各动作体力消耗

| 动作 | 基础消耗 | 关联技能 | 工具房减免 |
|------|---------|---------|-----------|
| till | 20 | farming | 14 |
| plant | 12 | farming | 8 |
| water | 10 | farming | 7 |
| harvest | 15 | farming | 10 |
| fertilize | 8 | farming | 6 |
| buy/sell | 2 | - | 2 |
| build | 25 | machinery | 18 |
| green_manure | 10 | farming | 7 |
| compost | 5 | processing | 4 |
| feed_animals | 8 | husbandry | 6 |
| water_animals | 5 | husbandry | 4 |
| collect_products | 10 | husbandry | 7 |
| slaughter | 20 | husbandry | 14 |
| spread_manure | 12 | farming | 8 |

### 6.3 体力恢复

| 行为 | 体力恢复 | 条件 |
|------|---------|------|
| 午间小睡 | +30 energy, +10 fatigue_clear | afternoon 日相，消耗 1 次行动 |
| 夜间睡眠 | 满恢复 energy，fatigue 清零 | evening→morning 过天（自动） |
| 吃饭 | +20 energy, hunger+40 | 消耗 storage 中任意食物作物 1 个 |
| 喝水 | hydration+50 | 消耗 1 次行动（免费） |

### 6.4 饥饿/脱水惩罚

```
hunger < 20 → energy消耗 ×1.5
hunger < 5  → energy消耗 ×2.0, 禁止重体力(till/build/slaughter)
hydration < 20 → energy消耗 ×1.3
fatigue > 80 → 所有 action 消耗 ×1.5
fatigue >= 100 → 只能 sleep。任何其他 action 返回 "体力耗尽，必须睡觉"
```

### 6.5 技能升级

每完成相关操作获得 XP：
- farming XP: till/plant/water/harvest/fertilize 每次 +5~15 XP
- husbandry XP: feed/water/collect/slaughter/breed 每次 +8~20 XP
- machinery XP: build 每次 +30 XP
- processing XP: process/compost 每次 +10 XP

升级阈值：Lv1→2: 100XP, Lv2→3: 250XP, Lv3→4: 500XP, Lv4→5: 1000XP

每升一级：该技能相关动作体力消耗 -10%，成功率/产出 +5%

### 6.6 工作日程（Agent 自主决策）

Agent 不需要被强制规定日程。体力/饥饿/口渴/疲劳这些约束会让它**自己发现**：
- "体力不够了，下午得睡一觉"
- "饿了，吃一个储物里的作物"
- "天快黑了，疲劳度高，今天就到这吧"

这比硬编码日程更有趣——Agent 要通过试错学会管理自己的体力。

---

## 实施顺序

1. **体力消耗+恢复**（嵌入现有 action 体系）
2. **饥饿/疲劳惩罚**（在 action 层检查）
3. **动物数据模型 + 建筑**（coop/barn/beehive）
4. **动物动作 + 每日循环**
5. **粪肥 + 蜜蜂授粉**
6. **繁殖 + 疾病**
7. **技能系统**
8. **更新 Agent 大脑**

缩减版：不做的
- ❌ 基因/性状遗传（GA 杂交已够）
- ❌ 传染病模型（个体健康衰减足够）
- ❌ 青贮饲料（一种 hay 就够了）
- ❌ 人工授精（公母配比足够有趣）
