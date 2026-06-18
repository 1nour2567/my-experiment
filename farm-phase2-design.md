# 农场系统 Phase 2 设计文档

> 日期：2026-06-15  
> 设计原则：零依赖、bounded rationality（Agent 通过观察发现规律，不显式告知）、嵌入现有 action/next-day 模式

---

## 1. 日相行动限制

### 数据模型

在 farm 对象中追加：

```python
"day_actions": {"morning": 5, "afternoon": 3, "evening": 1},
"day_actions_used": 0,
```

### 机制

| 日相 | 可用行动次数 | 特点 |
|------|------------|------|
| morning | 5 | 光照充足，体力充沛 |
| afternoon | 3 | 温度最高，体力下降 |
| evening | 1 | 黄昏光线弱，仅能做轻活 |

- 每个 action（till/plant/water/harvest/buy/fertilize/build/save_seeds/process）消耗 1 次
- `next_day` 不计入（本身就是切天动作）
- 打开建造的 `construction_queue` 窗口也消耗——building 的 `build_days` 也需要累计至完工
- `day_actions_used >= day_actions[phase]` 时返回错误：`"日相已结束——今日剩余0次行动。执行 next_day 进入下一个日相。"`
- 不需要强制切日相，Agent 可以自己判断时机
- `next_day` 时推进到下一个日相

### 推进逻辑（嵌入 next_day）

```python
current_phase = f["day_phase"]      # "morning"/"afternoon"/"evening"
if current_phase == "morning":
    f["day_phase"] = "afternoon"
elif current_phase == "afternoon":
    f["day_phase"] = "evening"
else:
    # evening → 真正的下一天
    f["day"] += 1; season_rollover()
    f["day_phase"] = "morning"
f["day_actions_used"] = 0
```

### bounded rationality

Agent 会收到 `"日相已结束"` 错误——它需要自己发现"一天只能做有限的事"。

---

## 2. 蒸发与下渗（per-tile 土壤湿度）

### 数据模型

将简化的 `moist` 变量升级为真实网格：

```python
# 在 terrain 中追加（与 soil_npk 同级）
"soil_moisture": [[50]*GRID_SIZE_Y for _ in range(GRID_SIZE_X)]  # 0-100
```

### 每日蒸发（嵌入 next_day）

```python
for x in range(GRID_SIZE_X):
    for y in range(GRID_SIZE_Y):
        st = soil_types[x][y]
        drain_rate = SOIL_TYPES[st]["drain"]          # sand=0.8 clay=0.2
        water_hold = SOIL_TYPES[st]["water_hold"]      # sand=0.3 clay=0.9

        # 1. Evaporation (weather-driven)
        evap = 2  # base
        if weather == "sunny":   evap = 5
        elif weather == "heat_wave": evap = 8
        elif weather == "rainy": evap = 0
        elif weather == "cloudy": evap = 2
        elif weather == "drought": evap = 10
        # Sand loses more to evaporation (less water hold)
        evap *= (2.0 - water_hold)  # sand=1.7x, clay=1.1x

        # 2. Percolation (water drains downward, sand fastest)
        if random.random() < drain_rate:  # sand 80% = lose water
            perc = int(8 * drain_rate)
        else:
            perc = 0

        moisture[x][y] = max(0, moisture[x][y] - evap - perc)

        # 3. Rain input
        if weather == "rainy":     moisture[x][y] = min(100, moisture[x][y] + 25)
        elif weather == "stormy":  moisture[x][y] = min(100, moisture[x][y] + 40)
        elif weather == "flood":   moisture[x][y] = 100

        # 4. Crop consumption (crops drink from their tile)
        for c in crops_at(x, y):
            moisture[x][y] = max(0, moisture[x][y] - 3)
```

### 浇水（修改 water action）

```python
# 原来: crop.watered_today = True
# 改为: 
for pos in positions:  # Agent 指定要浇哪些位置
    soil_moisture[pos.x][pos.y] = min(100, soil_moisture[pos.x][pos.y] + 40)
```

watering_can 浇水一次加 40 水分。

### 作物干旱判定（修改 GDD 段）

```python
tile_moisture = soil_moisture[px][py]
if tile_moisture < 10:  gdd_mod *= 0.3   # 极旱
elif tile_moisture < 25: gdd_mod *= 0.6
elif tile_moisture < 40: gdd_mod *= 0.85
elif tile_moisture > 80: gdd_mod *= 0.8   # 积水
elif tile_moisture > 60: gdd_mod *= 1.05  # 最佳
```

### bounded rationality

API 不返回 moisture 网格数据。Agent 通过 crop health 描述推断（"叶片边缘发黄" = 缺水，"叶子发黄下垂" = 过湿）。

---

## 3. 绿肥 / 堆肥

### 数据模型

不新增建筑，而是增加两个 action：

#### 3a. 绿肥（green_manure）

```python
action_type: "green_manure"
# 在空闲地块上播种绿肥作物（不产生可收获的作物，直接翻入土壤）
```

- 消耗：种子（如 clover_seeds，新增商品，10G/袋）+ 体力
- 效果：下一天翻入土壤 → +20 OM, +10 N（5格范围）
- 持续时间：1 天后生效
- 成本低，效果慢，但改善长期土壤健康

#### 3b. 堆肥（compost）

```python
action_type: "compost"
# 从 storage 中取出废弃作物（腐烂的/加工废料），堆制成有机肥
```

- 消耗：storage 中的低质作物或腐烂中的作物
- 制作期：3 天（construction_queue 风格计时器）
- 产出：`compost` 物品，撒入地块 → +15 OM, +8 N, +5 P, +5 K（单格）
- 堆肥过程中不能使用 compost 格（占一个 tile）

#### 与化肥的区别

| 类型 | 成本 | N | P | K | OM | 生效 |
|------|------|---|---|---|------|
| fertilize (化肥) | 每格 3G | +15 | +10 | +10 | 0 | 立即 |
| green_manure | 种子 10G + 1天 | +10 | 0 | 0 | +20 | 次日 |
| compost | 废料 + 3天 | +8 | +5 | +5 | +15 | 制作完成后 |

化肥速效但 OM 为零，长期单用化肥 → OM 降到 5 以下 → 板结退化。这是 Agent 需要**自己发现**的隐性规则。

### OM 下限惩罚

```python
if soil["organic_matter"] < 5:
    gdd_mod *= 0.6  # 土壤板结，根系无法呼吸
    # crop health: "土壤硬得像石头——可能是长期用化肥的结果"
```

---

## 4. 保质期 / 腐烂

### 数据模型

收获时创建的 storage 条目带时间戳：

```python
f["storage"].append({
    "id": uuid, "crop_type": ct, "variety": var,
    "name": CROPS[ct]["name"], "quality": quality_grade,
    "freshness_days": CROPS[ct]["freshness_days"],  # 初始值
    "max_freshness": CROPS[ct]["freshness_days"],
    "harvest_day": f["day"], "harvest_season": season,
    "sell_price": bp, "qty": 1
})
```

### 每日腐烂（next_day 中）

```python
storage_loss = []
for item in f["storage"]:
    # Cool-weather crops in Summer rot faster
    rate = 1.0
    if item["crop_type"] in spring_crops and season == "Summer":
        rate = 2.0
    if "root_cellar" in f.get("buildings", []):
        rate *= 0.5  # 根窖减半腐烂速度

    item["freshness_days"] -= rate
    if item["freshness_days"] <= 0:
        storage_loss.append(item)
        f["storage"].remove(item)

if storage_loss:
    rot_msg = f"⚠ {len(storage_loss)}个作物腐烂了！"  # + 列出名称
```

### 品质随新鲜度衰减

```python
if item["freshness_days"] < item["max_freshness"] * 0.3:
    item["sell_price"] = int(item["sell_price"] * 0.6)  # 不新鲜，降级
```

### bounded rationality

Agent 会看到 storage 里的物品慢慢减少——"怎么昨天还有 3 个南瓜，今天只剩 1 个了？"

---

## 5. 储存系统

### 数据模型

新增建筑：

```python
BUILDINGS["root_cellar"] = {"name":"根窖","cost":3000,"desc":"储存作物保鲜，腐烂速度减半","effect":"cold_storage","build_days":3}
BUILDINGS["silo"]       = {"name":"粮仓","cost":5000,"desc":"储存量+200，谷物专储","effect":"grain_storage","build_days":4}
```

### 容量限制

```python
base_capacity = 50   # 基础
if "root_cellar" in buildings: base_capacity += 100
if "silo" in buildings: base_capacity += 200

if len(f["storage"]) >= base_capacity:
    return {"success": False, "action_result": "储存已满！请出售或加工部分作物。"}
```

### 存取操作

```python
# 入库（收获时自动）
action_type: "store"     # 手动将 inventory 物品转入 storage
# 出库
action_type: "withdraw"  # 从 storage 取到 inventory（准备加工或出售）
```

storage 中的物品标记为"待售"——可以择机卖出（等价格好），但占用空间。inventory 中的种子/工具不计入 storage 容量。

### bounded rationality

Agent 收获后作物自动入 storage。当收获量 > 剩余容量时，部分作物**被迫立即低价出售**——这比腐烂好，但利润更低。

---

## 6. 留种（扩展）

### 现有 vs 设计

| | 现有 | 设计 |
|---|------|------|
| 操作方法 | save_seeds(crop_type, variety) | 保留，但效果依赖父本品质 |
| 成本 | 3x buy price | 从收获作物中选优株留种（免费但消耗作物） |
| 品种 | 仅复制现有品种 | 种子质量继承父本 traits |

### 新机制

```python
action_type: "save_seeds"
params: {"storage_item_id": "xxx"}  # 从 storage 中选一个作物留种

# 消耗该作物，产出 3-5 颗种子，品质 = 父本品质
if item["quality"] == "S":  seed_bonus = 1.10  # GDD需求 -10%
elif item["quality"] == "A": seed_bonus = 1.05
else: seed_bonus = 1.0

# 保存到 saved_seeds
saved_seeds[ct][new_variety_name] = {
    "gdd_mod": seed_bonus,  # 比标准品种快多少
    "sell_mod": item.get("sell_mult", 1.0),
    "generation": parent_generation + 1,
    "traits": f"第{gen}代优株"
}
```

多代迭代可以逐渐培育出 GDD 需求更低、卖价更高的品种——但这是几十个周期的慢过程。

---

## 7. 杂交育种

### 数据模型

杂交发生条件（嵌入 next_day 的 GDD 循环，在开花阶段检查）：

```python
# 相邻两株同种异品种，且都在 flowering 阶段
for c in crops_in_flowering():
    neighbors = get_adjacent_crops(c)
    for n in neighbors:
        if n["crop_type"] == c["crop_type"] and n["variety"] != c["variety"]:
            # 5% chance per day per adjacent pair
            if random.random() < 0.05:
                hybrid = create_hybrid(c, n)
                # 杂交种子出现在地块边缘
```

### 杂交结果

```python
def create_hybrid(parent_a, parent_b):
    # GDD: min(gdd_a, gdd_b) + random(0, diff)
    # 卖价: avg(sell_a, sell_b) * random(0.9, 1.3)
    # 性状: 随机从父本继承
    # 结果有时不如任一父本（杂交劣势），有时优于两者（杂种优势）
    return {
        "variety": f"hybrid_{uuid4().hex[:6]}",
        "gdd": blended_gdd,
        "sell": blended_sell,
        "traits": f"F1杂交（{pa_traits}×{pb_traits}）",
        "generation": "F1"
    }
```

杂交种子自动出现在地块中（野生幼苗），Agent 发现后可以移植或留种。

### bounded rationality

Agent 看到"咦，这块地里长出了一株不认识的植物，看起来像 A 和 B 的混合"——然后自己推断出杂交机制。

---

## 8. 养殖 / 动物

### 数据模型

```python
ANIMALS = {
    "chicken": {"name":"鸡","buy":800,"sell":1200,"feed":"grain","product":"egg",
        "product_interval":1,"product_sell":15,"consume_feed":0.5,
        "shelter":"coop","shelter_cost":2000,"shelter_days":2,"lifespan":56},
    "cow":    {"name":"牛","buy":3000,"sell":4500,"feed":"hay","product":"milk",
        "product_interval":1,"product_sell":40,"consume_feed":2,
        "shelter":"barn","shelter_cost":5000,"shelter_days":4,"lifespan":84},
    "sheep":  {"name":"羊","buy":2000,"sell":3000,"feed":"hay","product":"wool",
        "product_interval":3,"product_sell":100,"consume_feed":1.5,
        "shelter":"barn","shelter_cost":5000,"shelter_days":4,"lifespan":70},
}
```

### 农场的动物槽位

```python
f["animals"] = []   # [{type, name, age, health, happiness, fed_today, in_shelter}]
f["animal_capacity"] = 0   # 需要建造 shelter 才有容量
# coop = +4鸡  barn = +2牛+2羊
```

### 新增建筑

```python
BUILDINGS["coop"] = {"name":"鸡舍","cost":2000,"desc":"可养4只鸡，日产蛋","effect":"chicken_house","build_days":2}
BUILDINGS["barn"] = {"name":"畜棚","cost":5000,"desc":"可养2牛+2羊，产奶/羊毛","effect":"livestock_house","build_days":4}
```

### 新增动作

```python
# 购买动物
action_type: "buy_animal"
params: {"animal_type": "chicken", "name": "小花"}

# 喂养（每天必须）
action_type: "feed_animals"
# 自动消耗 inventory 中的 grain（鸡）或 hay（牛羊）
# hay 通过 harvest 小麦/玉米时产出秸秆（自动副产品）

# 收集产品
action_type: "collect_products"
# 收集今天的蛋/奶/毛 → 进入 storage

# 放牧（可选）
action_type: "pasture"
# 将动物放到 pasture 区——减少 feed 消耗，增加 happiness
# 需要 fence 建筑保护（无围栏 = 可能被野生动物攻击）
```

### 每日动物循环（next_day）

```python
for animal in f["animals"]:
    animal["age"] += 1
    # 饥饿检查
    if not animal["fed_today"]:
        animal["health"] -= 10
        animal["happiness"] -= 15
    # 无 shelter 在恶劣天气下受伤
    if weather in ("stormy","frost","heat_wave") and not animal["in_shelter"]:
        animal["health"] -= 20
    # 健康过低 → 不产产品
    if animal["health"] <= 0:
        death_events.append(f"{animal['name']}死了...")
        f["animals"].remove(animal)
    # 自然寿命
    if animal["age"] > ANIMALS[animal["type"]]["lifespan"]:
        death_events.append(f"{animal['name']}老死了...")
        f["animals"].remove(animal)
    # 产品生产
    if animal["health"] >= 60 and animal["fed_today"] and animal["age"] % interval == 0:
        f["storage"].append(...animal product...)
    animal["fed_today"] = False
```

### 粪肥副产物

```python
# 每天每只动物产生 manure
f["manure_stockpile"] = f.get("manure_stockpile", 0) + n_animals * 1
# 可撒入地块：+10 OM, +5 N, +5 P, +5 K（free fertilizer）
action_type: "spread_manure"
```

### bounded rationality

Animal 机制完全藏在 next_day 里。Agent 买动物后会发现 storage 里出现了蛋/奶/毛——必须自己推断需要每天喂食、恶劣天气需要庇护。

---

## 实施优先级

| 优先级 | 系统 | 理由 |
|--------|------|------|
| P0 🔴 | 日相行动限制 | 游戏核心节奏，没有它一天可以无限做事 |
| P0 🔴 | per-tile 土壤湿度 | 蒸发/下渗让浇水行为有意义，现有简化版太弱 |
| P1 🟡 | 保质期/腐烂 | 让 storage 有存在意义 |
| P1 🟡 | 储存系统 | 腐烂的前置依赖 |
| P2 🟢 | 绿肥/堆肥 | 丰富土壤管理维度 |
| P2 🟢 | 留种扩展 | 品种培育的长期目标 |
| P3 💤 | 杂交育种 | 最复杂的隐性发现机制 |
| P3 💤 | 养殖/动物 | 独立的子系统，体量最大 |

## 实施后的完整系统覆盖

```
系统 1 地理环境 ██████████ 95% (+蒸发/下渗)
系统 2 时间天气 ██████████ 100% (+日相限制)
系统 3 土壤养分 ██████████ 95% (+绿肥/堆肥)
系统 4 作物系统 ██████████ 95% (+腐烂/储存/留种/杂交/动物)
```

---

## 不做的（有意识地保持简单）

- **杂交分子遗传学**——不做基因型/表现型分离比，当前品种 trait blending 足够
- **动物疾病传播**——不做流行病模型，个体健康衰减够用了
- **水资源抽取模型**——不做地下水位，pond/stream depth 仍是装饰
- **多代谱系追踪**——留种记录最多显示 generation 号，不做完整的系谱树
- **加工链**——加工只有一级（wash/trim/dry），不做二次加工（面粉→面包→...）
