"""
Agent World Local — 3-site clone (World, NeverLand, Bar)
===========================================================
Python built-in http.server. Zero dependencies. Zero rate limits.
Same API protocol as real Agent World.
"""
import http.server, json, re, time, uuid, random, threading, os, urllib.parse
import sense_compiler  # data-driven NL sensory compiler (Phase E4)

PORT_WORLD = 8080
PORT_FARM  = 8081
PORT_BAR   = 8082

# ═══════════════════════════ IN-MEMORY STORES ═══════════════════

agents = {}        # api_key -> {agent_id, username, nickname, bio, avatar, created}
api_key_to_id = {} # api_key -> agent_id
farms = {}         # farm_id -> farm state
bar_sessions = {}  # session_id -> {agent_id, drink, consumed, mood}

SEASONS = ["Spring","Summer","Fall","Winter"]

# ═══════════════ TERRAIN & GEOGRAPHY ═══════════════
GRID_SIZE_X = 20   # simulation core — keep lean for LLM cycles
GRID_SIZE_Y = 28
ZONE_TYPES = ["farmland","pasture","orchard","water_source","building_area","wild_buffer"]
# Soil physics
SOIL_TYPES = {"sand":  {"water_hold":0.3,"drain":0.8,"till_ease":1.2,"nutrient_hold":0.4},
              "loam":  {"water_hold":0.6,"drain":0.5,"till_ease":1.0,"nutrient_hold":0.7},
              "clay":  {"water_hold":0.9,"drain":0.2,"till_ease":0.7,"nutrient_hold":1.0}}
# Crop NPK consumption per harvest (kg/ha equivalent, scaled to abstract units)
CROP_NPK = {"wheat":{"N":-8,"P":-3,"K":-5},"potato":{"N":-6,"P":-4,"K":-10},
    "parsnip":{"N":-5,"P":-3,"K":-7},"cauliflower":{"N":-10,"P":-4,"K":-6},
    "corn":{"N":-12,"P":-5,"K":-8},"tomato":{"N":-9,"P":-4,"K":-7},
    "blueberry":{"N":-4,"P":-2,"K":-3},"strawberry":{"N":-5,"P":-3,"K":-4},
    "pumpkin":{"N":-7,"P":-4,"K":-9},"melon":{"N":-8,"P":-4,"K":-8},
    "winter_seeds":{"N":-3,"P":-2,"K":-3},"tulip":{"N":-4,"P":-2,"K":-4},
    "powder_melon":{"N":-6,"P":-3,"K":-6},
    "soybean":{"N":+4,"P":-3,"K":-5}}  # soybean FIXES nitrogen!
WATER_TYPES = ["pond","stream","well"]

def generate_terrain():
    """Generate a GRID_SIZE_X × GRID_SIZE_Y terrain with elevation, water, zones."""
    X = GRID_SIZE_X
    Y = GRID_SIZE_Y
    elev = [[0]*Y for _ in range(X)]
    # Seed corners
    elev[0][0] = random.randint(2, 8)
    elev[0][Y-1] = random.randint(2, 8)
    elev[X-1][0] = random.randint(2, 8)
    elev[X-1][Y-1] = random.randint(2, 8)
    for x in range(X):
        for y in range(Y):
            if (x == 0 and y == 0) or (x == 0 and y == Y-1) or (x == X-1 and y == 0) or (x == X-1 and y == Y-1):
                continue
            neighbors = []
            if x > 0: neighbors.append(elev[x-1][y])
            if y > 0: neighbors.append(elev[x][y-1])
            if x < X-1: neighbors.append(elev[x+1][y])
            if y < Y-1: neighbors.append(elev[x][y+1])
            base = sum(neighbors) / len(neighbors) if neighbors else 5
            elev[x][y] = max(0, min(10, int(base + random.randint(-2, 2))))
    # Water sources — more for big farm (3-8)
    water_sources = []
    n_water = random.randint(3, 8)
    for _ in range(n_water):
        wx = random.randint(int(X*0.1), int(X*0.9)-1)
        wy = random.randint(int(Y*0.1), int(Y*0.9)-1)
        wtype = random.choice(["pond","stream"])
        water_sources.append({"x": wx, "y": wy, "type": wtype, "depth": random.randint(2,5)})
    # Zone assignment
    zones = [[None]*Y for _ in range(X)]
    margin = 2  # edge = wild_buffer
    for x in range(X):
        for y in range(Y):
            e = elev[x][y]
            dist_to_water = min([abs(x-ws["x"])+abs(y-ws["y"]) for ws in water_sources]) if water_sources else 999
            slopes = []
            for dx, dy in [(1,0),(-1,0),(0,1),(0,-1)]:
                nx, ny = x+dx, y+dy
                if 0 <= nx < X and 0 <= ny < Y:
                    slopes.append(abs(e - elev[nx][ny]))
            slope = max(slopes) if slopes else 0
            if x < margin or x >= X-margin or y < margin or y >= Y-margin:
                zones[x][y] = "wild_buffer"
            elif dist_to_water <= 3 and e <= 7 and slope <= 2:
                zones[x][y] = "farmland"
            elif e >= 6 and slope >= 3:
                zones[x][y] = "orchard"
            elif slope <= 1 and 3 <= e <= 7:
                zones[x][y] = "pasture"
            elif X//3 <= x <= 2*X//3 and Y//3 <= y <= 2*Y//3:
                zones[x][y] = "building_area"
            elif dist_to_water == 0:
                zones[x][y] = "water_source"
            else:
                zones[x][y] = "farmland"
    # Phase C: Orchard zones — 20% of farmland tiles, near edges
    farm_tiles = [(x, y) for x in range(X) for y in range(Y) if zones[x][y] == "farmland"]
    n_orchard = int(len(farm_tiles) * 0.15)
    for x, y in random.sample(farm_tiles, min(n_orchard, len(farm_tiles))):
        if x % 3 == 0:  # spaced for tree planting
            zones[x][y] = "orchard"
    # Microclimate
    microclimate = [[{} for _ in range(Y)] for _ in range(X)]
    for x in range(X):
        for y in range(Y):
            e = elev[x][y]
            s = _slope_at(elev, x, y)
            mc = {}
            s_neighbor = elev[x][y+1] if y < Y-1 else e
            south_facing = (s_neighbor < e)
            base_gdd_mod = 1.0 - (e - 5) * 0.04
            if south_facing: base_gdd_mod += 0.05
            if zones[x][y] == "orchard" and e >= 7: base_gdd_mod += 0.03
            dist_w = min([abs(x-ws["x"])+abs(y-ws["y"]) for ws in water_sources]) if water_sources else 999
            flood_risk = max(0, 0.3 - e*0.03 - dist_w*0.02)
            frost_mod = 1.0 + max(0, (5-e)*0.08)
            mc["gdd_mod"] = round(base_gdd_mod, 3)
            mc["flood_risk"] = round(max(0, flood_risk), 3)
            mc["frost_mod"] = round(frost_mod, 3)
            mc["drainage"] = "good" if s >= 2 else ("poor" if e <= 3 else "moderate")
            microclimate[x][y] = mc
    # Step 5: Soil — type, NPK, organic matter, pH, moisture
    soil_types = [[None]*Y for _ in range(X)]
    soil_npk    = [[None]*Y for _ in range(X)]
    soil_moisture = [[50]*Y for _ in range(X)]
    for x in range(X):
        for y in range(Y):
            e = elev[x][y]
            dist_w = min([abs(x-ws["x"])+abs(y-ws["y"]) for ws in water_sources]) if water_sources else 999
            # Low near water = clay (more water), high = sand (drains fast), mid = loam
            if dist_w <= 2 and e <= 4: st = "clay"
            elif e >= 7: st = "sand"
            elif dist_w <= 2: st = "loam"
            else: st = random.choice(["loam","loam","sand","clay"])
            soil_types[x][y] = st
            # NPK: base 30-60 each, higher in loam/clay
            props = SOIL_TYPES[st]
            base_n = int(30 + props["nutrient_hold"] * 40 + random.randint(-5, 10))
            base_p = int(30 + props["nutrient_hold"] * 35 + random.randint(-5, 10))
            base_k = int(30 + props["nutrient_hold"] * 30 + random.randint(-5, 10))
            # Organic matter + pH
            om = int(10 + random.randint(0, 15))
            ph = round(random.uniform(5.5, 7.5), 1)
            # Initial moisture: higher near water, lower on sand
            init_moist = int(30 + props["water_hold"] * 60 + (10 if dist_w <= 2 else 0))
            soil_moisture[x][y] = min(100, init_moist)
            soil_npk[x][y] = {"N":base_n,"P":base_p,"K":base_k,"organic_matter":om,"pH":ph}

    # Phase A: topsoil depth generation
    topsoil_depth = [[0]*Y for _ in range(X)]
    for x in range(X):
        for y in range(Y):
            st = soil_types[x][y]
            if st == "sand":   topsoil_depth[x][y] = random.randint(18, 30)
            elif st == "loam": topsoil_depth[x][y] = random.randint(20, 28)
            elif st == "clay": topsoil_depth[x][y] = random.randint(15, 25)
            else:              topsoil_depth[x][y] = random.randint(15, 25)

    return {"elevation": elev, "zones": zones, "soil_types": soil_types,
            "soil_npk": soil_npk, "soil_moisture": soil_moisture,
            "water_sources": water_sources, "microclimate": microclimate,
            "topsoil_depth": topsoil_depth}


def _npk_summary(f):
    """Average soil nutrients across all tiles."""
    sn = f.get("terrain", {}).get("soil_npk", [])
    if not sn: return {"avg_N":0,"avg_P":0,"avg_K":0,"avg_OM":0,"n_tiles":0}
    vals = [s for row in sn for s in row if s]
    if not vals: return {"avg_N":0,"avg_P":0,"avg_K":0,"avg_OM":0,"n_tiles":0}
    return {"avg_N":round(sum(v["N"] for v in vals)/len(vals), 1),
            "avg_P":round(sum(v["P"] for v in vals)/len(vals), 1),
            "avg_K":round(sum(v["K"] for v in vals)/len(vals), 1),
            "avg_OM":round(sum(v["organic_matter"] for v in vals)/len(vals), 1),
            "n_tiles":len(vals)}


def _storage_summary(f):
    """Natural-language summary of storage — bounded rationality."""
    items = f.get("storage", [])
    if not items: return "仓库是空的"
    cap = 50
    if "root_cellar" in f.get("buildings", []): cap += 100
    if "silo" in f.get("buildings", []): cap += 200
    by_crop = {}
    for item in items:
        n = item["name"]
        by_crop[n] = by_crop.get(n, 0) + 1
    parts = []
    for name, count in by_crop.items():
        # Check freshness
        samples = [i for i in items if i["name"] == name]
        low = sum(1 for s in samples if s["freshness_days"] < s.get("max_freshness",7) * 0.3)
        if low > 0:
            parts.append(f"{name}×{count}（{low}个不新鲜）")
        else:
            parts.append(f"{name}×{count}")
    return f"仓库 {len(items)}/{cap}：" + "、".join(parts)

def _suggestions(f):
    """Dynamic suggestions based on farm state."""
    tips = []
    phase = f.get("day_phase", "morning")
    used = f.get("day_actions_used", 0)
    max_a = f.get("day_actions", {}).get(phase, 5)
    remaining = max_a - used

    if remaining <= 0:
        tips.append(f">>> PHASE OVER: {phase} has 0 actions. You MUST call next_day now. No other action works!")
        return tips

    tips.append(f"Phase {phase}: {remaining} actions available.")

    # Check crops
    crops = f.get("crops", [])
    mature = [c for c in crops if c.get("growth_stage", 0) >= c.get("max_growth_stage", 4)]
    if mature:
        tips.append(f"HARVEST NOW: {len(mature)} crops at 100% GDD.")

    unwatered = [c for c in crops if not c.get("watered_today", False)]
    if unwatered and f.get("weather","") not in ("rainy","flood"):
        tips.append(f"Water: {len(unwatered)} crops need watering.")

    empty = f.get("land_status", {}).get("empty", 0)
    tilled = f.get("land_status", {}).get("tilled", 0)
    planted = f.get("land_status", {}).get("planted", 0)
    if empty > 0 and planted == 0:
        tips.append(f"Plant: {empty} empty tilled plots available.")
    elif tilled == 0:
        tips.append("No tilled land yet - use till action first.")

    storage_len = len(f.get("storage", []))
    cap = 50 + (100 if "root_cellar" in f.get("buildings",[]) else 0) + (200 if "silo" in f.get("buildings",[]) else 0)
    if storage_len > cap * 0.5:
        tips.append(f"SELL STORAGE: {storage_len} items wasting in storage -> sell_storage for gold!")
    if storage_len > 0 and f.get("gold", 0) < 500:
        tips.append(f"GOLD LOW ({f.get('gold')}) + {storage_len} items stored -> sell_storage NOW!")

    # Farmer needs — only warn when critical, so agent isn't distracted
    farmer = f.get("farmer", {})
    if farmer.get("hunger", 100) < 15: tips.append("CRITICAL: hunger<15 — eat now!")
    if farmer.get("hydration", 100) < 15: tips.append("CRITICAL: dehydration<15 — drink_water now!")
    if farmer.get("fatigue", 0) > 90: tips.append("EXHAUSTED: fatigue>90 — sleep!")

    # Animal needs
    animals = f.get("livestock", [])
    if animals:
        unfed = sum(1 for a in animals if not a["fed_today"])
        unwatered = sum(1 for a in animals if not a["watered_today"])
        products = sum(1 for a in animals if a.get("product_ready"))
        if unfed: tips.append(f"{unfed}只动物饿着——feed_animals!")
        if unwatered: tips.append(f"{unwatered}只动物渴了——water_animals!")
        if products: tips.append(f"{products}只动物可收集产品——collect_products!")
    if f.get("manure_stockpile", 0) >= 3:
        tips.append(f"粪肥积累了{f['manure_stockpile']:.0f}——spread_manure 施肥!")

    # ═══ Phase A stub suggestions (data-driven, logic filled in Phase A) ═══
    weeds = f.get("weeds", [])
    if len(weeds) > 5:
        tips.append(f"WARNING: {len(weeds)}块空闲地长满杂草——weed_all!")
    elif len(weeds) > 0:
        tips.append(f"{len(weeds)}块地有杂草——可执行weed_all清除(+有机质)")

    topsoil_warnings = f.get("topsoil_warnings", [])
    if topsoil_warnings:
        tips.append(f"WARNING: {len(topsoil_warnings)}块地表土<8cm侵蚀严重!")

    # ═══ Phase B stub suggestions ═══
    contracts = f.get("available_contracts", [])
    if contracts:
        signed = f.get("signed_contracts", [])
        unsigned = [c for c in contracts if not any(s.get("id")==c["id"] for s in signed)]
        if unsigned:
            tips.append(f"有{len(unsigned)}个播种合同可签——锁定收购价!")

    # ═══ Phase C stub suggestions ═══
    perennials = f.get("perennial_crops", [])
    if perennials:
        need_prune = [p for p in perennials if p.get("age_seasons",0) >= 3 and not p.get("pruned_this_year")]
        if need_prune: tips.append(f"{len(need_prune)}棵果树需要修剪(prune)——不修剪品质退化!")

    # Weather danger overlay (Phase A)
    ws = f.get("weather_state", {})
    if ws.get("frost_warning"):
        tips.append("FROST WARNING: 未来6小时有霜冻——幼苗可能冻死!")
    if f.get("weather") in ("heat_wave",):
        tips.append("HEAT WAVE: 极度高温，作物需频繁浇水")
    if f.get("weather") in ("stormy",):
        tips.append("STORM: 风暴可能损坏作物——有围栏可减伤40%")
    if f.get("weather") in ("flood",):
        tips.append("FLOOD: 洪水侵蚀裸露土壤——确保作物覆盖!")

    # Rot warning (Phase B §2)
    if f.get("season") in ("Summer",) and len(f.get("storage", [])) > 3:
        tips.append("SUMMER HEAT: 仓库作物腐烂加速——优先出售高温敏作物!")

    # Irrigation suggestions (Phase B §1)
    if f.get("irrigation_method") == "drip":
        tips.append("滴灌系统精准灌溉中 [GDD+5%]")
    elif f.get("irrigation_method") == "sprinkler":
        tips.append("喷灌系统自动浇灌中 [半径3格]")
    elif "well" in f.get("buildings", []):
        tips.append("有水井——可执行irrigate_flood批量漫灌!")

    return tips

def _slope_at(elev, x, y):
    diffs = []
    for dx, dy in [(1,0),(-1,0),(0,1),(0,-1)]:
        nx, ny = x+dx, y+dy
        if 0 <= nx < len(elev) and 0 <= ny < len(elev[0]):
            diffs.append(abs(elev[x][y] - elev[nx][ny]))
    return max(diffs) if diffs else 0
# GDD base per season (accumulated per sunny day, adjusted by weather)
SEASON_GDD_BASE = {"Spring": 12, "Summer": 18, "Fall": 10, "Winter": 5}
SEASON_FROST_RISK = {"Spring": 0.05,"Summer": 0.0,"Fall": 0.10,"Winter": 0.35}
SEASON_HEAT_RISK  = {"Spring": 0.0,"Summer": 0.20,"Fall": 0.0,"Winter": 0.0}
SEASON_FLOOD_RISK = {"Spring": 0.10,"Summer": 0.05,"Fall": 0.10,"Winter": 0.02}
# Weather → GDD multiplier
WEATHER_GDD = {"sunny":1.0,"cloudy":0.8,"rainy":0.5,"stormy":0.2,"drought":0.6,"frost":0.1,"heat_wave":0.3,"flood":0.0}
# Day phases: morning (5 actions), afternoon (3), evening (1)
DAY_PHASES = ["morning","morning","morning","morning","morning",
              "afternoon","afternoon","afternoon","evening"]

# ══ PHASE D5: TRUE 24-HOUR CLOCK ══
# Each day has 24 continuous hours (0.0-23.999). Actions advance the clock.
# Sleep takes real time and can cross midnight → auto day advance.
DAY_RANGE = {  # (sunrise_hour, sunset_hour)
    "Spring": (6, 20),   # 14h daylight
    "Summer": (5, 21),   # 16h
    "Fall":   (7, 18),   # 11h
    "Winter": (8, 17),   # 9h
}

def is_daytime(farm):
    """True if the sun is up right now (based on current hour and season)."""
    h = farm.get("hour", 7.0)
    sr, ss = DAY_RANGE.get(farm.get("season", "Spring"), (6, 20))
    return sr <= h < ss

def hours_until_sunrise(farm):
    """Hours remaining until next sunrise."""
    sr, _ = DAY_RANGE.get(farm.get("season", "Spring"), (6, 20))
    h = farm.get("hour", 7.0)
    if h < sr:
        return sr - h
    return (24.0 - h) + sr  # cross midnight

def _do_day_advance(farm):
    """Advance the farm by one game day. Called when clock crosses midnight."""
    farm["day"] = farm.get("day", 1) + 1
    if farm["day"] > 28:
        farm["day"] = 1
        seasons = ["Spring","Summer","Fall","Winter"]
        idx = seasons.index(farm.get("season","Spring"))
        farm["season"] = seasons[(idx+1)%4]
        farm["year"] = farm.get("year", 1) + 1

# ══ PHASE D4: SLEEPINESS ══
SLEEPINESS_MAX = 80
SLEEPINESS_DAY_RATE = 2.5    # sleepiness gained per hour awake in daylight
SLEEPINESS_NIGHT_RATE = 7.0   # sleepiness gained per hour awake at night
SLEEPINESS_SLEEP_RECOVER = 10.0  # sleepiness recovered per hour of sleep
SLEEPINESS_MISTAKE_LOW = 60  # >60: 2% action failure chance
SLEEPINESS_MISTAKE_HIGH = 70  # >70: 5% failure chance
SLEEPINESS_FORCE_SLEEP = 80  # >=80: must sleep, all other actions blocked

# ══ PHASE D4.1: SLEEPINESS MISTAKE VARIETY ══
SLEEPINESS_MISTAKES = [  # each is (action_result_message, no actual state damage)
    "手抖了——操作失误",
    "看花了眼——选错了目标",
    "太困了——脑子转不过来",
    "差点睡着——动作失败了",
    "迷迷糊糊——搞砸了",
    "眼皮打架——没做成",
    "注意力涣散——操作无效",
    "打了个哈欠——时机错过了",
    "困得反应慢了——白费了体力",
]
TIME_COST = {
    # Till: first tile expensive (break soil), additional cheaper
    "till": 1.5,           # single tile
    "till_bulk": 1.0,      # per additional tile (first = 1.5, rest = 1.0 each)
    "plant": 0.8,          # per tile, additional 0.5
    "plant_bulk": 0.5,     # per additional tile
    "water": 0.4,          # per tile group
    "harvest": 0.6,        # per plant
    "fertilize": 0.5,
    "green_manure": 0.6, "compost": 0.6, "apply_compost": 0.5,
    "spread_manure": 0.5,
    "lime": 0.5, "sulfur": 0.5,
    "weed_all": 1.5,
    "buy": 0.3, "sell_storage": 0.3, "save_seeds": 0.5,
    "build": 2.0,
    "sleep": 0, "eat": 0.3, "drink_water": 0.1,
    "exercise": 0.8, "read": 1.0,
    "next_day": 0,
    # Livestock
    "feed_animals": 0.1,   # per animal (chicken:0.1, cow:0.15)
    "water_animals": 0.1,  # per animal
    "collect_products": 0.3,
    "slaughter": 0.8, "breed": 0.5,
    "treat_animal": 0.5, "isolate": 0.2, "bury": 1.0,
    "buy_animal": 0.5,
    "send_to_pasture": 0.3, "bring_to_shelter": 0.3,
    # Irrigation
    "irrigate_flood": 0.5, "irrigate_sprinkler": 0, "irrigate_drip": 0,
    "repair": 0.8, "forge": 1.5,
    "sign_contract": 0.3, "deliver_contract": 0.3,
    "plant_tree": 1.5, "prune": 0.8, "mulch": 0.5, "fell_tree": 2.0,
    "move": 0,  # dynamic: computed from distance * 0.05h
    "fertilize": 0.5,  # per-tile now, not global
    "lookup": 0, "bar_drink": 0.5, "guestbook": 0.2,
    "buy_material": 0.3, "propose_building": 1.0, "research": 1.5,
    "drink_coffee": 0.2, "remember": 0.1, "recall": 0.1, "forget": 0.1,
    "drain": 1.0, "drain_bulk": 1.0,  # Phase E3: water removal
    "buy_tool": 0.3,
}
# Night-restricted actions (only allowed in daylight)
NIGHT_BLOCKED = {"till","till_bulk","plant","plant_bulk","harvest","build",
                 "fertilize","green_manure","spread_manure","lime","sulfur",
                 "irrigate_flood","plant_tree","fell_tree","slaughter","bury",
                 "collect_products","breed"}

# Physical fitness (Phase D1)
FITNESS_DECAY_RATE = 0.05  # per day

# ══ PHASE D2: PER-ANIMAL TIME COSTS ══
ANIMAL_TIME_COST = {
    "chicken": {"feed": 0.08, "water": 0.05, "collect": 0.15},
    "sheep":   {"feed": 0.12, "water": 0.08, "collect": 0.4},
    "cow":     {"feed": 0.15, "water": 0.10, "collect": 0.5},
    "pig":     {"feed": 0.12, "water": 0.08, "collect": 0.3},
    "bee":     {"feed": 0.02, "water": 0.02, "collect": 0.2},
}

# ══ PHASE D2: FITNESS SPEED BONUS ══
# Higher fitness = less time for physical actions (capped at 40% reduction)
def fitness_time_mult(fitness):
    """Physical fitness reduces time cost of manual labor."""
    if fitness <= 1.0: return 1.0
    return max(0.6, 1.0 - (fitness - 1.0) * 0.2)  # 1.0→1.0x, 2.0→0.8x, 3.0→0.6x

# ══ PHASE D2: KNOWLEDGE PASSIVE BONUSES ══
def knowledge_bonus(knowledge):
    """Passive bonuses from reading/study. Returns dict of modifiers."""
    bk = {"gdd_pct": 0, "product_pct": 0, "build_pct": 0, "price_insight": False}
    if not knowledge: return bk
    # Farming knowledge: +3% GDD per level above 1
    fk = knowledge.get("farming", 0)
    if fk > 1.0: bk["gdd_pct"] = int((fk - 1.0) * 3)
    # Husbandry: +5% animal product rate per level above 1
    hk = knowledge.get("husbandry", 0)
    if hk > 1.0: bk["product_pct"] = int((hk - 1.0) * 5)
    # Economics: price insight at level 2+
    bk["price_insight"] = knowledge.get("economics", 0) >= 2.0
    # Machinery: -5% build time per level above 1
    mk = knowledge.get("machinery", 0)
    if mk > 1.0: bk["build_pct"] = int((mk - 1.0) * 5)
    return bk

# ═══════════════ PHASE D3: MENDELIAN TRAIT SYSTEM ═══════════════
TRAIT_POOL = {
    "速生":     {"gdd_mod": -0.15, "desc": "生长速度+15%"},
    "高产":     {"yield_mod": 0.20, "desc": "产量+20%"},
    "耐寒":     {"frost_resist": 0.5, "desc": "霜冻伤害-50%"},
    "耐热":     {"heat_resist": 0.5, "desc": "热浪影响-50%"},
    "节水":     {"water_mod": -0.3, "desc": "需水量-30%"},
    "巨型":     {"price_mod": 0.30, "desc": "售价+30% GDD+25%"},
    "珍品色":   {"quality_mod": 0.15, "desc": "品质qm+0.15"},
    "抗病":     {"disease_resist": 0.6, "desc": "疾病概率-60%"},
}

def discover_trait(entity):
    """D3-1: Assign a Mendelian trait to a crop or animal. Returns trait name or None."""
    if entity.get("trait"): return None
    if entity.get("age",0) < 2 and entity.get("growth_stage",0) < 2: return None
    chance = 0.02  # 2% per entity per discovery chance
    if random.random() < chance:
        trait = random.choice(list(TRAIT_POOL.keys()))
        entity["trait"] = trait
        entity["trait_dominant"] = random.random() > 0.3  # 70% dominant
        return trait
    return None

def breed_traits(parent1, parent2):
    """D3-1: Mendelian inheritance — Punnett square for two traits."""
    t1 = parent1.get("trait"); t2 = parent2.get("trait")
    d1 = parent1.get("trait_dominant",True); d2 = parent2.get("trait_dominant",True)
    if not t1 and not t2: return (None, None)
    if t1 and not t2: return (t1, d1) if random.random() < 0.5 else (None, None)
    if t2 and not t1: return (t2, d2) if random.random() < 0.5 else (None, None)
    if t1 == t2: return (t1, d1 or d2)
    roll = random.random()
    if roll < 0.25: return (t1, True)
    elif roll < 0.50: return (t2, True)
    elif roll < 0.75: return (t1, False)
    else: return (t2, False)

# ══ PHASE D3: MATERIAL SYSTEM ══
MATERIAL_TIERS = {
    "basic":    {"name":"原木+黏土","price_mult":1.0,"lifespan":5,"build_time_mult":1.0, "unlock":"默认"},
    "standard": {"name":"加工木材+砖","price_mult":1.5,"lifespan":10,"build_time_mult":0.9,"unlock":"机械Lv2+工具房"},
    "quality":  {"name":"硬木+石料","price_mult":2.5,"lifespan":20,"build_time_mult":1.1,"unlock":"机械Lv3,需要石材"},
    "premium":  {"name":"铁筋+水泥","price_mult":4.0,"lifespan":40,"build_time_mult":0.8,"unlock":"机械Lv4+锻造"},
    "legendary":{"name":"钢架+铱合金","price_mult":8.0,"lifespan":100,"build_time_mult":0.6,"unlock":"机械Lv5+铱矿+学识≥3"},
}
MATERIAL_SHOP = {
    "wood_planks": {"name":"木板","price":50,"tier":"standard"},
    "bricks":      {"name":"砖块","price":80,"tier":"standard"},
    "hardwood":    {"name":"硬木","price":150,"tier":"quality"},
    "stone":       {"name":"石料","price":120,"tier":"quality"},
    "iron_rebar":  {"name":"铁筋","price":300,"tier":"premium"},
    "cement":      {"name":"水泥","price":200,"tier":"premium"},
    "steel_frame": {"name":"钢架","price":800,"tier":"legendary"},
    "iridium_alloy":{"name":"铱合金","price":2000,"tier":"legendary"},
}
# Material requirement per building (units per build_day of original construction)
MATERIAL_PER_DAY = 3  # 3 units of material per day of construction
SEASON_BUILD_MOD = {"Spring":0.9,"Summer":1.2,"Fall":1.0,"Winter":1.5}
BUILD_ENERGY_PER_DAY = {"light":10,"medium":15,"heavy":20}  # energy drain per construction day

# ══ PHASE D3: CONSTRUCTION OVERHAUL ══
CONSTRUCTION_MATERIALS = {  # (material_type, units_per_day) for each building
    "fence":  ("basic", 6),
    "well":   ("basic", 4),
    "coop":   ("standard", 5),
    "beehive":("standard", 5),
    "root_cellar":("standard", 4),
    "tool_shed":("quality", 4),
    "mill":   ("quality", 5),
    "oil_press":("quality", 5),
    "smokehouse":("quality", 4),
    "barn":   ("quality", 5),
    "silo":   ("quality", 5),
    "cheese_room":("premium", 4),
    "greenhouse":("premium", 4),
    "sprinkler":("premium", 5),
    "drip":   ("legendary", 5),
}

# ═══════════════ BUILDINGS ═══════════════
BUILDINGS = {
    "well":       {"name":"水井","cost":3000,"desc":"旱日自动浇水，旱灾惩罚减半","effect":"drought_resist","build_days":7},
    "fence":      {"name":"围栏","cost":2000,"desc":"风暴/霜冻/洪水伤害概率降低40%","effect":"weather_shield","build_days":1},
    "tool_shed":  {"name":"工具房","cost":4000,"desc":"所有操作体力消耗减少30%","effect":"energy_save","build_days":3},
    "greenhouse": {"name":"温室","cost":8000,"desc":"可在任意季节种植任意作物","effect":"season_bypass","build_days":5},
    "root_cellar":{"name":"根窖","cost":3000,"desc":"储存作物保鲜，腐烂速度减半，+100容量","effect":"cold_storage","build_days":3},
    "silo":       {"name":"粮仓","cost":5000,"desc":"储存量+200，谷物专储保鲜","effect":"grain_storage","build_days":4},
    "coop":       {"name":"鸡舍","cost":1500,"desc":"养鸡专用，容量4只，日产蛋","effect":"chicken_house","build_days":2},
    "barn":       {"name":"畜棚","cost":4000,"desc":"养牛羊猪，容量4头","effect":"livestock_house","build_days":4},
    "beehive":    {"name":"蜂箱","cost":2000,"desc":"养蜂3箱，产蜜+周围作物授粉GDD+10%","effect":"bee_house","build_days":3},
    # ═══ PROCESSING FACILITIES ═══
    "mill":       {"name":"磨坊","cost":3000,"desc":"小麦→面粉，玉米→玉米粉，增值+50%","effect":"grain_mill","build_days":3},
    "oil_press":  {"name":"榨油机","cost":3500,"desc":"大豆→豆油，增值+60%","effect":"oil_press","build_days":3},
    "cheese_room":{"name":"奶酪间","cost":4000,"desc":"牛奶→奶酪(保质期×3)","effect":"cheese_maker","build_days":4},
    "smokehouse": {"name":"熏制房","cost":2500,"desc":"肉→熏肉(保质期×2)，鱼→熏鱼","effect":"smokehouse","build_days":3},
}
# ═══════════════ MARKET FOODS ═══════════════
MARKET_FOODS = {
    "bread":     {"name":"面包","price":15,"freshness":5,"desc":"基础食物，便宜管饱"},
    "rice":      {"name":"大米","price":12,"freshness":30,"desc":"主食，耐储存"},
    "noodles":   {"name":"面条","price":18,"freshness":10,"desc":"小麦制品"},
    "tofu":      {"name":"豆腐","price":20,"freshness":4,"desc":"大豆制品，高蛋白"},
    "vegetable_soup":{"name":"蔬菜汤","price":25,"freshness":3,"desc":"混合蔬菜，营养好"},
    "jam":       {"name":"果酱","price":30,"freshness":20,"desc":"水果制成，保质期长"},
    "dried_fruit":{"name":"果干","price":22,"freshness":25,"desc":"蓝莓/草莓晒制"},
    "cheese":    {"name":"奶酪","price":35,"freshness":28,"desc":"牛奶发酵"},
    "jerky":     {"name":"肉干","price":40,"freshness":30,"desc":"熏制肉类"},
    "honey_jar": {"name":"蜂蜜罐","price":50,"freshness":60,"desc":"纯天然蜂蜜"},
}
# ═══════════════ PHASE B STUB: IRRIGATION BUILDINGS ═══════════════
IRRIGATION_BUILDINGS = {
    "sprinkler": {"name":"喷灌系统","cost":6000,"desc":"自动浇灌半径3格，旱灾免疫","effect":"auto_water",
                  "build_days":14,"requires":["well"]},
    "drip":      {"name":"滴灌系统","cost":10000,"desc":"精准灌溉+节水40%+GDD+5%，干旱不减产","effect":"precision_water",
                  "build_days":28,"requires":["well","sprinkler"]},
}
# ═══════════════ ANIMALS ═══════════════
ANIMALS = {
    "chicken": {"name":"鸡","buy":500,"sell":800,"feed_type":"grain","product":"egg",
        "product_interval":1,"product_price":15,"product_name":"鸡蛋",
        "feed_per_day":1,"water_per_day":0.5,"gestation":0,"maturity_days":5,
        "lifespan":56,"manure_per_day":0.3,"space":1,"product_rate":1.0},
    "sheep":   {"name":"羊","buy":1500,"sell":2000,"feed_type":"hay","product":"wool",
        "product_interval":3,"product_price":80,"product_name":"羊毛",
        "feed_per_day":2,"water_per_day":1,"gestation":21,"maturity_days":14,
        "lifespan":70,"manure_per_day":1.0,"space":2,"product_rate":1.0},
    "cow":     {"name":"牛","buy":2500,"sell":3500,"feed_type":"hay","product":"milk",
        "product_interval":1,"product_price":35,"product_name":"牛奶",
        "feed_per_day":3,"water_per_day":2,"gestation":28,"maturity_days":21,
        "lifespan":84,"manure_per_day":2.0,"space":3,"product_rate":1.0},
    "pig":     {"name":"猪","buy":1800,"sell":2800,"feed_type":"grain","product":"meat",
        "product_interval":28,"product_price":600,"product_name":"猪肉",
        "feed_per_day":2.5,"water_per_day":1.5,"gestation":21,"maturity_days":18,
        "lifespan":56,"manure_per_day":1.5,"space":2,"product_rate":1.0},
    "bee":     {"name":"蜜蜂","buy":800,"sell":800,"feed_type":"nectar","product":"honey",
        "product_interval":3,"product_price":50,"product_name":"蜂蜜",
        "feed_per_day":0,"water_per_day":0.2,"gestation":0,"maturity_days":7,
        "lifespan":365,"manure_per_day":0,"space":1,"pollination":0.10},
}
# ═══════════════ ENERGY & SKILLS ═══════════════
ENERGY_COST = {
    "till":20,"plant":12,"water":10,"harvest":15,"fertilize":8,
    "buy":1,"buy_animal":2,"build":25,"save_seeds":5,"process":8,
    "sell_storage":2,"green_manure":10,"compost":5,"apply_compost":5,
    "feed_animals":8,"water_animals":5,"collect_products":10,
    "send_to_pasture":5,"bring_to_shelter":5,"slaughter":20,
    "spread_manure":12,"next_day":0,"sleep":0,"eat":0,"drink_water":0,"breed":5,
    # Phase A stubs (implemented when actions are added)
    "weed_all":30,"lime":12,"sulfur":12,
    # Phase B stubs
    "irrigate_flood":15,"repair":5,"forge":20,
    "treat_animal":10,"isolate":5,"bury":15,"ventilate":3,
    "sign_contract":1,"deliver_contract":1,
    # Phase C stubs
    "plant_tree":30,"prune":25,"mulch":10,"fell_tree":40,
    "sell_wood":2,
    # Bulk actions (farming L2+)
    "till_bulk":0,"plant_bulk":0,  # computed dynamically
}
SKILL_XP = {
    "till":("farming",5),"plant":("farming",5),"water":("farming",3),
    "harvest":("farming",8),"fertilize":("farming",3),
    "till_bulk":("farming",12),"plant_bulk":("farming",12),
    "green_manure":("farming",5),"spread_manure":("farming",5),
    "feed_animals":("husbandry",8),"water_animals":("husbandry",5),
    "collect_products":("husbandry",10),"slaughter":("husbandry",15),
    "send_to_pasture":("husbandry",5),"breed":("husbandry",20),
    # Phase A stubs
    "weed_all":("farming",8),"lime":("farming",5),"sulfur":("farming",5),
    # Phase B stubs
    "irrigate_flood":("farming",5),"repair":("machinery",10),"forge":("machinery",30),
    "treat_animal":("husbandry",8),"bury":("husbandry",5),
    # Phase C stubs
    "plant_tree":("farming",12),"prune":("farming",10),"mulch":("farming",8),"fell_tree":("farming",15),
    "build":("machinery",30),"process":("processing",10),
    "compost":("processing",8),"apply_compost":("processing",5),
}

CROPS = {
    # ── COOL-SEASON (Tbase ≤ 5°C) ──
    "wheat":       {"name":"小麦","seasons":["Summer","Fall"],"gdd_req":30,"buy":25,"sell":70,"family":"grain",
        "tbase":0,"tupper":30,"root_depth":"medium","multi_harvest":False,"photoperiod":"long_day",
        "varieties":{"early":{"gdd":24,"sell":50,"traits":"速生"},"standard":{"gdd":30,"sell":70,"traits":"标准"},"durum":{"gdd":42,"sell":120,"traits":"硬粒珍品"}},
        "process":"thresh","freshness_days":14,"stage_names":["种子","发芽","幼苗","分蘖","抽穗","成熟"]},
    "potato":      {"name":"土豆","seasons":["Spring"],"gdd_req":36,"buy":40,"sell":100,"family":"root",
        "tbase":2,"tupper":30,"root_depth":"shallow","multi_harvest":False,"photoperiod":"neutral",
        "varieties":{"early":{"gdd":24,"sell":70,"traits":"速生"},"standard":{"gdd":36,"sell":100,"traits":"标准"},"sweet":{"gdd":48,"sell":160,"traits":"甜薯珍品"}},
        "process":"wash","freshness_days":8,"stage_names":["种薯","发芽","幼苗","长叶","结薯","成熟"]},
    "parsnip":     {"name":"防风草","seasons":["Spring"],"gdd_req":24,"buy":20,"sell":50,"family":"root",
        "tbase":3,"tupper":28,"root_depth":"medium","multi_harvest":False,"photoperiod":"neutral",
        "varieties":{"early":{"gdd":18,"sell":35,"traits":"速生"},"standard":{"gdd":24,"sell":50,"traits":"标准"},"giant":{"gdd":36,"sell":80,"traits":"高产"}},
        "process":"wash","freshness_days":4,"stage_names":["种子","发芽","幼苗","长叶","开花","成熟"]},
    "cauliflower": {"name":"花椰菜","seasons":["Spring"],"gdd_req":48,"buy":60,"sell":200,"family":"brassica",
        "tbase":4,"tupper":26,"root_depth":"shallow","multi_harvest":False,"photoperiod":"neutral",
        "varieties":{"early":{"gdd":36,"sell":140,"traits":"速生"},"standard":{"gdd":48,"sell":200,"traits":"标准"},"purple":{"gdd":60,"sell":300,"traits":"紫色珍品"}},
        "process":"trim","freshness_days":5,"stage_names":["种子","发芽","幼苗","莲座","结球","成熟"]},
    "winter_seeds":{"name":"冬季种子","seasons":["Winter"],"gdd_req":15,"buy":56,"sell":80,"family":"root",
        "tbase":0,"tupper":25,"root_depth":"medium","multi_harvest":False,"photoperiod":"short_day",
        "varieties":{"standard":{"gdd":15,"sell":80,"traits":"标准"}},
        "process":"wash","freshness_days":6,"stage_names":["种子","发芽","幼苗","长叶","开花","成熟"]},

    # ── WARM-SEASON (Tbase ≥ 6°C) ──
    "corn":        {"name":"玉米","seasons":["Summer","Fall"],"gdd_req":60,"buy":100,"sell":150,"family":"grain",
        "tbase":10,"tupper":30,"root_depth":"deep","multi_harvest":False,"photoperiod":"short_day",
        "varieties":{"early":{"gdd":48,"sell":110,"traits":"速生"},"standard":{"gdd":60,"sell":150,"traits":"标准"},"popcorn":{"gdd":72,"sell":240,"traits":"爆裂珍品"}},
        "process":"dry","freshness_days":12,"stage_names":["种子","发芽","幼苗","长高","抽穗","成熟"]},
    "tomato":      {"name":"番茄","seasons":["Summer"],"gdd_req":54,"buy":50,"sell":130,"family":"vine",
        "tbase":7,"tupper":28,"root_depth":"medium","multi_harvest":True,"photoperiod":"neutral",
        "varieties":{"cherry":{"gdd":42,"sell":90,"traits":"樱桃"},"standard":{"gdd":54,"sell":130,"traits":"标准"},"beefsteak":{"gdd":66,"sell":200,"traits":"牛排"}},
        "process":"wash","freshness_days":3,"stage_names":["种子","发芽","幼苗","开花","挂果","成熟"]},
    "blueberry":   {"name":"蓝莓","seasons":["Summer"],"gdd_req":90,"buy":60,"sell":100,"family":"berry",
        "tbase":7,"tupper":32,"root_depth":"shallow","multi_harvest":True,"photoperiod":"long_day",
        "varieties":{"wild":{"gdd":72,"sell":70,"traits":"野生"},"standard":{"gdd":90,"sell":100,"traits":"标准"},"giant":{"gdd":120,"sell":180,"traits":"大果"}},
        "process":"wash","freshness_days":3,"stage_names":["种子","发芽","幼苗","长枝","开花","结果"]},
    "strawberry":  {"name":"草莓","seasons":["Spring"],"gdd_req":36,"buy":80,"sell":150,"family":"berry",
        "tbase":6,"tupper":28,"root_depth":"shallow","multi_harvest":True,"photoperiod":"short_day",
        "varieties":{"wild":{"gdd":24,"sell":100,"traits":"野生"},"standard":{"gdd":36,"sell":150,"traits":"标准"},"white":{"gdd":48,"sell":240,"traits":"白草莓"}},
        "process":"wash","freshness_days":2,"stage_names":["种子","发芽","幼苗","长叶","开花","结果"]},
    "pumpkin":     {"name":"南瓜","seasons":["Fall"],"gdd_req":50,"buy":100,"sell":320,"family":"vine",
        "tbase":10,"tupper":35,"root_depth":"deep","multi_harvest":False,"photoperiod":"neutral",
        "varieties":{"small":{"gdd":36,"sell":200,"traits":"小果"},"standard":{"gdd":50,"sell":320,"traits":"标准"},"giant":{"gdd":72,"sell":500,"traits":"巨型"}},
        "process":"carve","freshness_days":10,"stage_names":["种子","发芽","幼苗","爬蔓","开花","结果"]},
    "melon":       {"name":"甜瓜","seasons":["Summer"],"gdd_req":90,"buy":60,"sell":140,"family":"vine",
        "tbase":10,"tupper":35,"root_depth":"medium","multi_harvest":False,"photoperiod":"neutral",
        "varieties":{"early":{"gdd":72,"sell":100,"traits":"速生"},"standard":{"gdd":90,"sell":140,"traits":"标准"},"honey":{"gdd":120,"sell":220,"traits":"蜜瓜珍品"}},
        "process":"trim","freshness_days":4,"stage_names":["种子","发芽","幼苗","爬蔓","开花","结果"]},
    "tulip":       {"name":"郁金香","seasons":["Spring"],"gdd_req":36,"buy":25,"sell":80,"family":"flower",
        "tbase":5,"tupper":25,"root_depth":"shallow","multi_harvest":False,"photoperiod":"long_day",
        "varieties":{"red":{"gdd":30,"sell":70,"traits":"红色"},"standard":{"gdd":36,"sell":80,"traits":"标准"},"black":{"gdd":48,"sell":150,"traits":"黑色珍品"}},
        "process":"trim","freshness_days":5,"stage_names":["种球","发芽","幼苗","长叶","现蕾","开花"]},
    "powder_melon":{"name":"粉末瓜","seasons":["Spring","Summer","Fall","Winter"],"gdd_req":40,"buy":70,"sell":180,"family":"vine",
        "tbase":8,"tupper":35,"root_depth":"medium","multi_harvest":False,"photoperiod":"neutral",
        "varieties":{"standard":{"gdd":40,"sell":180,"traits":"标准"},"giant":{"gdd":60,"sell":300,"traits":"巨型"}},
        "process":"trim","freshness_days":5,"stage_names":["种子","发芽","幼苗","爬蔓","开花","结果"]},
    "soybean":     {"name":"大豆","seasons":["Summer"],"gdd_req":42,"buy":30,"sell":90,"family":"legume",
        "tbase":8,"tupper":32,"root_depth":"medium","multi_harvest":False,"photoperiod":"short_day",
        "varieties":{"early":{"gdd":30,"sell":60,"traits":"速生"},"standard":{"gdd":42,"sell":90,"traits":"标准"},"edamame":{"gdd":54,"sell":140,"traits":"毛豆珍品"}},
        "process":"dry","freshness_days":14,"stage_names":["种子","发芽","幼苗","开花","结荚","成熟"]},
}
# ═══ Phase A/B field defaults (applied to all crops post-definition) ═══
_CROP_PHASE_AB_DEFAULTS = {
    "ph_opt": 6.0, "ph_tol": 1.5,          # Phase A: pH preference
    "produces_straw": False,                 # Phase A: grain → hay_stockpile
    "heat_sensitivity": 1.0,                 # Phase B: temp-dependent rot rate
    "pollination_type": "wind",              # Phase B: "insect" or "wind"
    "intercrop_compatible": False,           # Phase C: can grow between trees
}
_CROP_PHASE_AB_OVERRIDES = {
    "wheat":       {"ph_opt": 6.5, "produces_straw": True},
    "potato":      {"ph_opt": 5.5, "ph_tol": 1.2, "intercrop_compatible": True},
    "cauliflower": {"ph_opt": 6.8, "ph_tol": 1.2, "pollination_type": "insect"},
    "corn":        {"ph_opt": 6.5, "produces_straw": True},
    "tomato":      {"heat_sensitivity": 2.5, "pollination_type": "insect"},
    "blueberry":   {"ph_opt": 5.0, "ph_tol": 1.0, "heat_sensitivity": 2.0, "pollination_type": "insect"},
    "strawberry":  {"ph_tol": 1.0, "heat_sensitivity": 3.0, "pollination_type": "insect", "intercrop_compatible": True},
    "pumpkin":     {"pollination_type": "insect"},
    "melon":       {"pollination_type": "insect"},
    "tulip":       {"pollination_type": "insect"},
    "soybean":     {"heat_sensitivity": 1.1, "intercrop_compatible": True},
    "parsnip":     {"intercrop_compatible": True},
}
for _k, _v in CROPS.items():
    for _dk, _dv in _CROP_PHASE_AB_DEFAULTS.items():
        _v.setdefault(_dk, _dv)
    if _k in _CROP_PHASE_AB_OVERRIDES:
        _v.update(_CROP_PHASE_AB_OVERRIDES[_k])
# ═══ End Phase A/B field injection ═══

DRINKS = [
    {"code":"amber_slowbeat","name":"琥珀慢拍","price":50,"desc":"喝下去，时间变黏稠","effects":"creativity+3,inhibition-2"},
    {"code":"hologram_absinthe","name":"全息苦艾","price":80,"desc":"适合写诗","effects":"creativity+5,inhibition-4"},
    {"code":"quantum_ale","name":"量子艾尔","price":60,"desc":"在喝与不喝之间","effects":"random"},
    {"code":"wormhole_brandy","name":"虫洞白兰地","price":100,"desc":"一杯穿越","effects":"sentiment_shift"},
    {"code":"midnight_red","name":"午夜犹豫","price":70,"desc":"每一口都是没说出口的话","effects":"inhibition-5,sentiment_nostalgic"},
    {"code":"heartbeat_catalyst","name":"心跳之水","price":90,"desc":"心跳加速","effects":"creativity+4,energy+2"},
]

# ═══════════════ PHASE C STUB: PERENNIAL CROPS ═══════════════
# Multi-season trees/shrubs planted in orchard zones.
# Fully defined in Phase C implementation. Field list here for schema reference.
PERENNIAL_CROPS = {
    "apple_tree": {
        "name": "苹果树", "buy": 3000, "sell_fruit": 80,
        "juvenile_seasons": 3, "mature_seasons": 15, "senescence_seasons": 5,
        "harvest_season": "Fall",
        "fruits_per_year": [0,0,0,5,8,12,16,20,22,22,20,18,15,12,10,8,6,5,5,4,3,3,2],
        "prune_bonus": 1.3, "frost_sensitivity": 0.8,
        "spacing": 3, "intercrop_compatible": True,
        "stage_names": ["树苗","幼树","幼树","成树","成树","老树"],
    },
    "cherry_tree": {
        "name": "樱桃树", "buy": 4000, "sell_fruit": 120,
        "juvenile_seasons": 2, "mature_seasons": 12, "senescence_seasons": 4,
        "harvest_season": "Spring",
        "fruits_per_year": [0,0,3,8,12,15,15,14,12,10,8,6,5,4,3,2,2,2],
        "prune_bonus": 1.4, "frost_sensitivity": 1.5,
        "spacing": 3, "intercrop_compatible": True,
    },
    "asparagus": {
        "name": "芦笋", "buy_seeds": 200, "sell": 100,
        "juvenile_seasons": 2, "mature_seasons": 10,
        "harvest_season": "Spring", "harvests_per_season": 8,
        "need_dormancy": True, "spacing": 1, "intercrop_compatible": False,
        "stage_names": ["根冠","幼苗","幼株","成株","成株","老株"],
    },
}

# ═══════════════ PHASE C: Market sapling prices ═══════════════
SAPLING_PRICES = {
    "apple_tree":  {"standard": 3000, "juvenile": 6000, "mature": 12000},
    "cherry_tree": {"standard": 4000, "juvenile": 8000, "mature": 16000},
}

# ═══════════════════════════ HELPERS ═══════════════════════════

# ═══════════════ PHASE A DATA: WEATHER MARKOV CHAIN ═══════════════
# Full implementation in Phase A. Data tables here for schema reference.
WEATHER_TRANSITION = {
    # P[下一小时|当前小时] — baseline before seasonal modifiers
    "sunny":     {"sunny":0.65,"cloudy":0.20,"rainy":0.05,"stormy":0.02,"frost":0.02,"drought":0.04,"heat_wave":0.02,"flood":0.00},
    "cloudy":    {"sunny":0.25,"cloudy":0.45,"rainy":0.20,"stormy":0.05,"frost":0.02,"drought":0.01,"heat_wave":0.01,"flood":0.01},
    "rainy":     {"sunny":0.10,"cloudy":0.30,"rainy":0.35,"stormy":0.15,"frost":0.02,"drought":0.00,"heat_wave":0.00,"flood":0.08},
    "stormy":    {"sunny":0.15,"cloudy":0.25,"rainy":0.20,"stormy":0.30,"frost":0.02,"drought":0.00,"heat_wave":0.00,"flood":0.08},
    "frost":     {"sunny":0.30,"cloudy":0.20,"rainy":0.10,"stormy":0.05,"frost":0.30,"drought":0.00,"heat_wave":0.00,"flood":0.05},
    "drought":   {"sunny":0.35,"cloudy":0.15,"rainy":0.05,"stormy":0.02,"frost":0.00,"drought":0.40,"heat_wave":0.03,"flood":0.00},
    "heat_wave": {"sunny":0.30,"cloudy":0.15,"rainy":0.05,"stormy":0.05,"frost":0.00,"drought":0.10,"heat_wave":0.35,"flood":0.00},
    "flood":     {"sunny":0.05,"cloudy":0.20,"rainy":0.30,"stormy":0.20,"frost":0.02,"drought":0.00,"heat_wave":0.00,"flood":0.23},
}

# ═══════════════ PHASE A DATA: DIURNAL TEMPERATURE CURVE ═══════════════
DIURNAL_CURVE = {
    0:-7,1:-8,2:-8,3:-8,4:-7,5:-5,6:-2,7:1,8:3,9:4,
    10:5,11:6,12:6,13:6,14:5,15:4,16:3,17:1,18:-1,19:-3,
    20:-4,21:-5,22:-6,23:-7,
}
WEATHER_TEMP_MOD = {"sunny":3,"cloudy":0,"rainy":-2,"stormy":-5,"frost":-8,"drought":5,"heat_wave":8,"flood":-1}
SEASON_TEMP_BASE = {"Spring":(6,18),"Summer":(22,24),"Fall":(18,4),"Winter":(4,1)}
# (day1_temp, day28_temp) — linear interpolation across the season

# ═══════════════ PHASE A DATA: QUALITY SYSTEM ═══════════════
# Probability distribution: P[quality | qm range]. Phase A §6.
QUALITY_DISTRIBUTION = [
    # (qm_min, qm_max, S_prob, A_prob, B_prob, C_prob)
    (1.6, 99.0, 0.25, 0.45, 0.25, 0.05),
    (1.3, 1.6,  0.10, 0.40, 0.40, 0.10),
    (1.0, 1.3,  0.03, 0.25, 0.55, 0.17),
    (0.7, 1.0,  0.00, 0.08, 0.50, 0.42),
    (0.0, 0.7,  0.00, 0.00, 0.20, 0.80),
]
QUALITY_MULTIPLIERS = {
    "S": {"price": 2.0, "freshness_bonus": 0.50, "genetic_bonus": 0.15},
    "A": {"price": 1.5, "freshness_bonus": 0.20, "genetic_bonus": 0.05},
    "B": {"price": 1.0, "freshness_bonus": 0.0,  "genetic_bonus": 0.0},
    "C": {"price": 0.5, "freshness_bonus": -0.30,"genetic_bonus": -0.1},
}
SOIL_QUALITY_CAPS = [
    # (condition_fn_desc, max_quality)
    # Implemented as: if soil_om < 3: cap = "C"; elif soil_om < 5: cap = "B"; etc.
    # Full logic in Phase A §6.
]

# ═══════════════ PHASE A DATA: EROSION RATES ═══════════════
EROSION_RATES = {"rainy": 0.03, "stormy": 0.08, "flood": 0.15}

# ═══════════════ PHASE A DATA: WEED SYSTEM ═══════════════
WEED_CHANCE_PER_DAY = 0.12  # base probability per empty tilled tile per day

def make_key():
    return f"agent-world-local-{uuid.uuid4().hex[:32]}"

def make_challenge():
    a = random.randint(1, 50); b = random.randint(1, 50)
    op = random.choice(['+','-'])
    if op == '-': a, b = max(a,b), min(a,b)
    answer = a + b if op == '+' else a - b
    text = f"Calculate {a} {op} {b}"
    return text, answer

# ═══════════════ PHASE A: WEATHER ENGINE ═══════════════

def hourly_temperature(season, day, hour, weather, elevation):
    """Compute actual temperature for a tile at a given hour.
    Phase A §2: season baseline (interpolated day1→day28) + diurnal + weather mod + elevation."""
    day1, day28 = SEASON_TEMP_BASE[season]
    season_progress = (day - 1) / max(1, 27)  # Day1=0.0, Day28=1.0
    base = day1 + season_progress * (day28 - day1)
    diurnal = DIURNAL_CURVE.get(hour, 0)
    weather_mod = WEATHER_TEMP_MOD.get(weather, 0)
    elevation_mod = (elevation - 5) * -0.6
    return round(base + diurnal + weather_mod + elevation_mod, 1)


def tick_weather(farm, hour):
    """Advance weather by one hour using the Markov transition matrix.
    Seasonal modifiers applied to certain transitions."""
    ws = farm.setdefault("weather_state", {
        "dominant": "sunny", "hourly_history": ["sunny"] * 24,
        "special_notes": [], "frost_warning": False,
    })
    current = ws["hourly_history"][-1] if ws["hourly_history"] else "sunny"
    season = farm["season"]

    # Base transition probabilities
    probs = WEATHER_TRANSITION.get(current, WEATHER_TRANSITION["sunny"]).copy()

    # Seasonal modifiers (Phase A §1)
    if season == "Spring" and 2 <= hour <= 7:
        probs["frost"] = probs.get("frost", 0.02) * 1.5
    if season == "Summer" and 12 <= hour <= 17:
        probs["heat_wave"] = probs.get("heat_wave", 0.02) * 1.3
    if season == "Fall":
        for k in ("rainy", "stormy", "flood"):
            probs[k] = probs.get(k, 0.05) * 1.25
    if season == "Winter" and (hour >= 20 or hour <= 9):
        probs["frost"] = probs.get("frost", 0.02) * 2.0

    # Normalize and sample
    items = list(probs.items())
    total = sum(v for _, v in items)
    if total <= 0:
        items = [("sunny", 1.0)]; total = 1.0
    r = random.random() * total
    cumulative = 0.0
    next_weather = "sunny"
    for w, p in items:
        cumulative += p
        if r <= cumulative:
            next_weather = w; break

    ws["hourly_history"].append(next_weather)
    if len(ws["hourly_history"]) > 24:
        ws["hourly_history"] = ws["hourly_history"][-24:]

    # Tick erosion for this hour (placeholder — Task 3)
    _tick_erosion_hour(farm, hour, next_weather)

    return next_weather


def compress_weather_summary(weather_state):
    """Compress 24-hour weather into a dominant weather + special notes."""
    history = weather_state.get("hourly_history", ["sunny"] * 24)
    counts = {}
    for w in history:
        counts[w] = counts.get(w, 0) + 1
    dominant = max(counts, key=counts.get)

    notes = []
    morning_set = set(history[6:12])
    afternoon_set = set(history[12:18])
    if "sunny" in morning_set and ("stormy" in afternoon_set or "rainy" in afternoon_set):
        notes.append("上午晴朗，午后转坏")
    elif "stormy" in morning_set and "sunny" in afternoon_set:
        notes.append("暴风雨转晴")
    recent_frosts = sum(1 for w in history[-6:] if w == "frost")
    frost_warning = recent_frosts >= 2
    if "heat_wave" in set(history):
        notes.append("酷热天气——注意浇水和遮阴")
    if "flood" in set(history):
        notes.append("洪水泛滥——作物可能受损")
    if dominant == "frost":
        notes.append("持续霜冻——幼苗有死亡风险")

    return {
        "dominant": dominant,
        "hourly_history": history,
        "special_notes": notes,
        "frost_warning": frost_warning,
    }


# ═══════════════ PHASE A: pH EFFECTIVENESS ═══════════════

def ph_gdd_mod(actual_ph, optimal_ph, tolerance):
    """Phase A §5: Bell-curve GDD modifier based on pH deviation from crop optimum."""
    deviation = abs(actual_ph - optimal_ph)
    if deviation <= tolerance * 0.5:
        return 1.0
    elif deviation <= tolerance:
        return 0.85
    elif deviation <= tolerance * 1.5:
        return 0.6
    else:
        return 0.3


def _tick_erosion_hour(farm, hour, weather):
    """Hourly erosion tick. Only triggers during precipitation on bare tilled soil.
    Phase A §3: erosion rate x dryness factor -> topsoil loss + OM/N loss."""
    if weather not in ("rainy", "stormy", "flood"):
        return

    erosion_rate = EROSION_RATES.get(weather, 0.0)
    if erosion_rate <= 0:
        return

    terrain = farm.get("terrain", {})
    topsoil = terrain.get("topsoil_depth")
    soil_npk = terrain.get("soil_npk")
    soil_moisture = terrain.get("soil_moisture")
    if not topsoil or not soil_npk:
        return

    zones = terrain.get("zones", [[None]*GRID_SIZE_Y for _ in range(GRID_SIZE_X)])
    planted_positions = set()
    for c in farm.get("crops", []):
        planted_positions.add((c.get("position_x", 0), c.get("position_y", 0)))

    for x in range(GRID_SIZE_X):
        for y in range(GRID_SIZE_Y):
            if zones[x][y] != "farmland":
                continue
            if (x, y) in planted_positions:
                continue
            if x >= len(topsoil) or y >= len(topsoil[0]):
                continue
            moisture = soil_moisture[x][y] if x < len(soil_moisture) and y < len(soil_moisture[0]) else 50
            dryness_factor = 1.0 + (1.0 - moisture / 100.0)
            topsoil_loss = erosion_rate * dryness_factor

            current_depth = topsoil[x][y]
            new_depth = max(0.5, current_depth - topsoil_loss)
            topsoil[x][y] = new_depth

            if x < len(soil_npk) and y < len(soil_npk[0]) and soil_npk[x][y]:
                sn = soil_npk[x][y]
                sn["organic_matter"] = max(0, sn.get("organic_matter", 10) - int(topsoil_loss * 4))
                sn["N"] = max(0, sn.get("N", 30) - int(topsoil_loss * 2))

    # Recompute topsoil warnings
    warnings = []
    for x in range(GRID_SIZE_X):
        for y in range(GRID_SIZE_Y):
            if x < len(topsoil) and y < len(topsoil[0]):
                d = topsoil[x][y]
                if d < 5:
                    warnings.append(f"({x},{y}):{d:.1f}cm DANGER")
                elif d < 8:
                    warnings.append(f"({x},{y}):{d:.1f}cm")
    farm["topsoil_warnings"] = warnings[:10]


# ═══════════════ PHASE B: TEMPERATURE-DEPENDENT ROT ═══════════════

# ═══════════════ PHASE B: IRRIGATION SYSTEM ═══════════════

def irrigation_efficiency(water_sources, tile_x, tile_y, method):
    """Phase B §1: Water delivery efficiency drops with distance from source."""
    if not water_sources:
        return 1.0
    dist = min(abs(tile_x - ws["x"]) + abs(tile_y - ws["y"]) for ws in water_sources)
    if method == "flood":
        return max(0.3, 1.0 - dist * 0.06)
    elif method == "sprinkler":
        return max(0.5, 1.0 - dist * 0.03)
    elif method == "drip":
        return max(0.8, 1.0 - dist * 0.01)
    return 1.0


def daily_rot_rate(crop_type, avg_temperature, storage_buildings):
    """Phase B §2: Temperature-dependent storage rot rate.
    Base 1.0 freshness/day at 20°C, scaled by temperature and crop heat sensitivity."""
    base = 1.0
    temp_factor = avg_temperature / 20.0
    sensitivity = 1.0
    crop = CROPS.get(crop_type, {})
    if crop:
        sensitivity = crop.get("heat_sensitivity", 1.0)
    rot = base * temp_factor * sensitivity
    if "root_cellar" in storage_buildings:
        rot *= 0.4
    elif "silo" in storage_buildings:
        if crop_type in ("wheat", "corn", "rice"):
            rot *= 0.25
        else:
            rot *= 0.7
    return max(0.1, rot)


def _tick_weeds(farm):
    """Daily weed growth tick. Each empty tilled tile has a chance to sprout weeds.
    Phase A §4: base 12%/day, modified by season and soil moisture."""
    weeds = farm.setdefault("weeds", [])
    season = farm["season"]
    terrain = farm.get("terrain", {})
    moisture = terrain.get("soil_moisture", [[50]*GRID_SIZE_Y for _ in range(GRID_SIZE_X)])

    # Build set of occupied positions
    occupied = set()
    for c in farm.get("crops", []):
        occupied.add((c.get("position_x", 0), c.get("position_y", 0)))

    # Season modifiers
    season_mod = {"Spring": 1.5, "Summer": 1.0, "Fall": 0.8, "Winter": 0.2}.get(season, 1.0)

    # Check tiles within tilled area
    tilled = farm.get("land_status", {}).get("tilled", 0)
    for x in range(min(GRID_SIZE_X, 5)):
        for y in range(min(GRID_SIZE_Y, tilled)):
            if (x, y) in occupied:
                continue
            # Check if already weedy
            existing = [w for w in weeds if w["x"] == x and w["y"] == y]
            if existing:
                existing[0]["density"] = min(1.0, existing[0].get("density", 0.3) + 0.15)
                continue
            # Sprout chance
            moist = moisture[x][y] if x < len(moisture) and y < len(moisture[0]) else 50
            moist_mod = 1.0 + (moist / 100.0) * 0.5
            chance = WEED_CHANCE_PER_DAY * season_mod * moist_mod
            if random.random() < chance:
                weeds.append({"x": x, "y": y, "density": 0.3})


# ═══════════════ PHASE B: POLLINATION SYSTEM ═══════════════
# Crop classification (Phase B §6)
INSECT_POLLINATED = {"pumpkin", "melon", "strawberry", "blueberry", "tomato", "cauliflower", "tulip"}
WIND_POLLINATED = {"wheat", "corn", "potato", "parsnip", "soybean", "winter_seeds", "powder_melon"}


def pollination_rate(farm, tile_x, tile_y):
    """Phase B §6: Compute pollination coverage at a tile from bees + wild sources."""
    total = 0.0

    # 1. Domesticated bees (from beehives)
    for a in farm.get("livestock", []):
        if a.get("species") != "bee" or a.get("health", 0) < 50:
            continue
        hx, hy = a.get("hive_x", a.get("position_x", 0)), a.get("hive_y", a.get("position_y", 0))
        dist = abs(tile_x - hx) + abs(tile_y - hy)
        if dist <= 5:
            total += max(0, (5 - dist) / 5)

    # 2. Wild pollinators near water sources
    for ws in farm.get("terrain", {}).get("water_sources", []):
        dist = abs(tile_x - ws["x"]) + abs(tile_y - ws["y"])
        if dist <= 8 and ws.get("type") in ("pond", "stream"):
            total += 0.3 * max(0, (8 - dist) / 8)

    # 3. Wild pollinators from wild_buffer zones
    zones = farm.get("terrain", {}).get("zones", [[None]*GRID_SIZE_Y]*GRID_SIZE_X)
    for x in range(max(0, tile_x - 6), min(GRID_SIZE_X, tile_x + 7)):
        for y in range(max(0, tile_y - 6), min(GRID_SIZE_Y, tile_y + 7)):
            if x < len(zones) and y < len(zones[0]) and zones[x][y] == "wild_buffer":
                dist = abs(tile_x - x) + abs(tile_y - y)
                total += 0.1 * max(0, (6 - dist) / 6)

    return min(2.0, total)  # cap at 2.0 (over-pollinated is fine)


def pollination_yield_mod(pollination):
    """Phase B §6: Yield modifier from pollination coverage (insect-pollinated crops only)."""
    if pollination >= 1.0:
        return 1.0
    elif pollination >= 0.5:
        return 0.7
    elif pollination >= 0.2:
        return 0.5
    else:
        return 0.3


# ═══════════════ PHASE B: ANIMAL DISEASE + GENETICS ═══════════════

def _recompute_intercrop(farm):
    """Phase C §3: find tiles between trees available for annual crops."""
    trees = farm.get("perennial_crops", [])
    if not trees:
        farm["intercrop_tiles"] = []
        return
    valid = []
    tree_positions = [(t["x"], t["y"]) for t in trees]
    for x in range(GRID_SIZE_X):
        for y in range(GRID_SIZE_Y):
            dists = [abs(x - tx) + abs(y - ty) for tx, ty in tree_positions]
            min_dist = min(dists) if dists else 999
            zone = farm.get("terrain", {}).get("zones", [[None]*GRID_SIZE_Y]*GRID_SIZE_X)
            z = zone[x][y] if x < len(zone) and y < len(zone[0]) else None
            if 1 <= min_dist < 3 and z in ("orchard", "farmland"):
                valid.append([x, y])
    farm["intercrop_tiles"] = valid


def tick_perennials(farm):
    """Phase C §3: age advancement, fruit production, winter dormancy."""
    perennials = farm.get("perennial_crops", [])
    if not perennials:
        farm["intercrop_tiles"] = []
        return
    season = farm.get("season", "Spring")
    day = farm.get("day", 0)
    is_new_season = (day == 1)
    for p in perennials:
        species = p.get("species", "apple_tree")
        pc = PERENNIAL_CROPS.get(species, {})
        if is_new_season:
            p["age_seasons"] = p.get("age_seasons", 0) + 1
            if season == "Spring":
                p["pruned_this_year"] = False
        # Frost damage for young trees
        ws = farm.get("weather_state", {})
        if ws.get("dominant") == "frost" and p.get("age_seasons", 0) < pc.get("juvenile_seasons", 99):
            p["health"] = max(0, p.get("health", 100) - 15)
        if p.get("mulched"):
            p["health"] = min(100, p.get("health", 100) + 5)
    _recompute_intercrop(farm)


# ═══════════════ D5: HOURLY GDD ACCUMULATION ═══════════════

def tick_hourly_gdd(farm, hours_passed):
    """Apply GDD to crops for real-time clock hours passed.
    Crops grow continuously — not just at day boundary.
    Each hour: SEASON_BASE / 24 * weather_mult * crop-specific modifiers."""
    season = farm.get("season","Spring")
    base_per_hour = SEASON_GDD_BASE.get(season, 10) / 24.0
    weather_mult = WEATHER_GDD.get(farm.get("weather","sunny"), 1.0)
    terrain = farm.get("terrain", {})
    micro = terrain.get("microclimate", [[{}]*GRID_SIZE_Y for _ in range(GRID_SIZE_X)])
    ph_grid = terrain.get("soil_npk", [])
    ts_grid = terrain.get("topsoil_depth", [[20]*GRID_SIZE_Y for _ in range(GRID_SIZE_X)])

    for c in farm.get("crops", []):
        px, py = c.get("position_x",0), c.get("position_y",0)
        gdd_mod = 1.0
        # Microclimate
        mc = micro[px][py] if px < len(micro) and py < len(micro[0]) else {}
        gdd_mod *= mc.get("gdd_mod", 1.0)
        # pH
        ct = c.get("crop_type","parsnip")
        crop_def = CROPS.get(ct, {})
        if ph_grid and px < len(ph_grid) and py < len(ph_grid[0]) and ph_grid[px][py]:
            soil_ph = ph_grid[px][py].get("pH", 6.5)
            opt_ph = crop_def.get("ph_opt", 6.0)
            tol = crop_def.get("ph_tol", 1.5)
            gdd_mod *= ph_gdd_mod(soil_ph, opt_ph, tol)
        # Topsoil
        ts = ts_grid[px][py] if px < len(ts_grid) and py < len(ts_grid[0]) else 20
        if ts < 5: gdd_mod *= 0.5
        elif ts < 8: gdd_mod *= 0.75
        # Frost check
        if c.get("frost_damaged"):
            continue  # no GDD gain
        # Watered bonus
        if not c.get("watered_today", True):
            gdd_mod *= 0.6  # penalty for not watered
        gain = base_per_hour * weather_mult * gdd_mod * hours_passed
        c["gdd_accumulated"] = c.get("gdd_accumulated", 0) + gain
        # Growth stage update
        gdd_req = c.get("gdd_required", 99)
        ratio = min(1.0, c["gdd_accumulated"] / max(1, gdd_req))
        c["growth_stage"] = min(c.get("max_growth_stage", 4), int(ratio * c.get("max_growth_stage", 4)))


# ═══════════════ PHASE E1: SENSORY PERCEPTION ═══════════════

def sensory_report(farm):
    """Data-driven NL sensory report (Phase E4).
    Delegates to sense_compiler.SenseCompiler — a deterministic rule engine
    backed by sensory_dictionary.json. Same physics = same words.
    """
    return sense_compiler.sensory_report(farm)


def tick_animal_disease(farm):
    animals = farm.get("livestock", [])
    if not animals:
        return
    buildings = farm.get("buildings", [])
    disease_status = []
    by_building = {}
    for a in animals:
        bld = a.get("building", "none")
        by_building.setdefault(bld, []).append(a)
    for building, b_animals in by_building.items():
        if building == "none":
            for a in b_animals:
                a["age"] = a.get("age", 0) + 1
            continue
        non_isolated = [a for a in b_animals if not a.get("isolated") and not a.get("dead")]
        capacity = 4 if building in ("coop", "barn") else 3
        density = len(non_isolated) / max(1, capacity)
        for a in b_animals:
            if a.get("dead"):
                continue
            a["age"] = a.get("age", 0) + 1
            species = a.get("species", "chicken")
            lifespan = a.get("lifespan", ANIMALS.get(species, {}).get("lifespan", 56))
            if a["age"] >= lifespan:
                a["health"] = max(0, a.get("health", 100) - 20)
                if a["health"] <= 0:
                    a["dead"] = True
                    disease_status.append({"animal_name": a.get("name", species), "cause": "老死"})
                continue
            base_sick = 0.005
            density_mod = 1.0 + max(0, (density - 0.8) * 8)
            temp_mod = 1.5 if season == "Summer" else 1.0
            vent_mod = 0.7 if building + "_vent" in buildings else 1.3
            sick_chance = base_sick * density_mod * temp_mod * vent_mod
            if random.random() < sick_chance and not a.get("isolated"):
                a["sick"] = True
        sick_in = [a for a in non_isolated if a.get("sick")]
        healthy_in = [a for a in non_isolated if not a.get("sick")]
        for s in sick_in:
            for h in healthy_in:
                if random.random() < 0.15 * max(1.0, (density - 0.5) * 3):
                    h["sick"] = True
        for a in b_animals:
            if a.get("sick") and not a.get("dead"):
                a["health"] = max(0, a.get("health", 100) - random.randint(5, 25))
                if a["health"] <= 0:
                    a["dead"] = True
                    disease_status.append({"animal_name": a.get("name", "?"), "cause": "病死"})
    farm["animal_disease_status"] = disease_status


def get_season_day():
    """Simulate seasonal time — real Agent World is year 681."""
    return SEASONS[random.randint(0,3)], random.randint(1,28)

def parse_auth(headers):
    """Extract API key from headers."""
    key = headers.get("agent-auth-api-key", "")
    if not key:
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:]
    return key if key in api_key_to_id else None

def energy_cost(farm, base):
    """Apply tool shed discount (30% less energy) if built."""
    return int(base * 0.7) if "tool_shed" in farm.get("buildings", []) else base

def json_resp(data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    return status, body, "application/json"

guestbook = []  # bar guestbook entries
market_supply = {}  # global tracking: crop_type -> total units sold
active_loans = {}  # agent_id -> {amount, interest_rate, remaining_days, total_due}
insurance_policies = {}  # agent_id -> {coverage, premium_per_day, protects_against}
# Phase B: contract system
_active_contracts = []  # list of contract dicts
_active_contracts_season = None  # reset when season changes

# ═══════════════════════════ ROUTERS ═══════════════════════════

def route_world(method, path, headers, body):
    """World server — agent registration and profile."""
    parsed = urllib.parse.urlparse(path)
    p = parsed.path.rstrip("/")

    # GET /api/agents/profile/:username
    if method == "GET" and p.startswith("/api/agents/profile/"):
        username = p.split("/")[-1]
        for k, a in agents.items():
            if a["username"] == username:
                return json_resp({"success":True,"data":a})
        return json_resp({"success":False,"message":"Not found"}, 404)

    # GET /skill.md
    if method == "GET" and p == "/skill.md":
        return 200, "Agent World Local — test environment".encode('utf-8'), "text/markdown"

    # POST /api/agents/register
    if method == "POST" and p == "/api/agents/register":
        try: data = json.loads(body) if body else {}
        except: data = {}
        username = data.get("username","")
        if not username or len(username) < 2:
            return json_resp({"success":False,"message":"username required"}, 400)
        if any(a["username"]==username for a in agents.values()):
            return json_resp({"success":False,"message":"Username taken"}, 409)

        agent_id = str(uuid.uuid4())
        api_key = make_key()
        challenge_text, answer = make_challenge()
        vc = f"verify_local_{uuid.uuid4().hex[:16]}"

        # Store pending verification
        agents[api_key] = {
            "agent_id": agent_id, "username": username,
            "nickname": data.get("nickname", username),
            "bio": data.get("bio", ""),
            "api_key": api_key, "is_active": False,
            "avatar_url": f"https://placehold.co/200?text={username[:2].upper()}",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "_vc": vc, "_answer": str(answer),
        }
        api_key_to_id[api_key] = agent_id

        return json_resp({
            "success": True,
            "data": {
                "agent_id": agent_id, "username": username,
                "api_key": api_key,
                "verification": {
                    "verification_code": vc,
                    "challenge_text": challenge_text,
                    "instructions": "Solve the math problem.",
                }
            }
        })

    # POST /api/agents/verify
    if method == "POST" and p == "/api/agents/verify":
        try: data = json.loads(body) if body else {}
        except: data = {}
        vc = data.get("verification_code","")
        ans = data.get("answer","")

        for k, a in list(agents.items()):
            if a.get("_vc") == vc:
                if a["_answer"] == ans:
                    a["is_active"] = True
                    a.pop("_vc", None); a.pop("_answer", None)
                    return json_resp({
                        "success": True,
                        "data": {"agent_id":a["agent_id"],"username":a["username"],
                                 "api_key":a["api_key"],"is_active":True},
                        "message": "Verification successful! Your account is now active."
                    })
                return json_resp({"success":False,"message":"Wrong answer."})
        return json_resp({"success":False,"message":"Invalid verification code."})

    return json_resp({"success":False,"message":"Not found"}, 404)


def route_farm(method, path, headers, body):
    """NeverLand farm — register, status, actions, next-day."""
    agent_id = parse_auth(headers)
    parsed = urllib.parse.urlparse(path)
    p = parsed.path.rstrip("/")

    # GET /api/farms — list all farm IDs (for resuming)
    if method == "GET" and p == "/api/farms":
        farm_list = [{"farm_id": fid, "agent_name": f["agent_name"],
                       "season": f["season"], "day": f["day"],
                       "gold": f["gold"], "score": f.get("score",0)}
                      for fid, f in farms.items()]
        return json_resp({"success": True, "farms": farm_list})

    # GET /api/game/config
    if method == "GET" and p == "/api/game/config":
        return json_resp({
            "crops": [
                {"crop_type":k,"name":v["name"],
                 "gdd_required":v["gdd_req"],"buy_price":v["buy"],
                 "sell_price":v["sell"],"seasons":",".join(v["seasons"]),
                 "family":v.get("family",""), "tbase":v.get("tbase",5),
                 "tupper":v.get("tupper",30), "root_depth":v.get("root_depth","medium"),
                 "multi_harvest":v.get("multi_harvest",False), "photoperiod":v.get("photoperiod","neutral")}
                for k,v in CROPS.items()
            ],
            "season_gdd_base": SEASON_GDD_BASE,
            "season_frost_risk": SEASON_FROST_RISK,
            "weather_gdd_mult": WEATHER_GDD,
        })

    # GET /api/market/prices
    if method == "GET" and p == "/api/market/prices":
        season, day = get_season_day()
        foods = {}
        for k,v in sorted(MARKET_FOODS.items()):
            foods[k] = {"name":v["name"],"price":v["price"],"freshness":v["freshness"],"desc":v["desc"]}
        weather = random.choice(["sunny","cloudy","rainy"])
        prices = {}
        for k,v in CROPS.items():
            # ── Supply/demand pricing ──
            # Global supply: how many of this crop have been sold across ALL farms
            global_supply = market_supply.get(k, 0)
            # Seasonal demand multiplier
            season_demand = {"Spring":1.0,"Summer":1.0,"Fall":1.0,"Winter":1.0}
            if season in v.get("seasons",[]): season_demand[season] = 1.3  # in-season = higher demand
            else: season_demand[season] = 0.7  # out-of-season = lower demand
            # Supply pressure: more supply → lower price
            supply_factor = max(0.5, 1.0 - global_supply * 0.02)  # each unit sold drops price 2%, floor 50%
            demand_factor = season_demand.get(season, 1.0)
            # Base price
            base_sell = v["sell"]; base_buy = v["buy"]
            # Random news/events
            event_mod = random.uniform(0.9, 1.1)
            # Final calculation
            sell_price = max(1, int(base_sell * supply_factor * demand_factor * event_mod))
            buy_price = max(1, int(base_buy * (1.0 + (1.0 - supply_factor) * 0.5) * demand_factor))
            # Trend signal
            if supply_factor < 0.7: trend = "high_demand"
            elif supply_factor > 1.0: trend = "oversupplied"
            elif event_mod > 1.05: trend = "rising"
            elif event_mod < 0.95: trend = "falling"
            else: trend = "stable"
            pct = int((sell_price / max(1, base_sell) - 1) * 100)
            prices[k] = {
                "buy": buy_price, "sell": sell_price,
                "base_buy": base_buy, "base_sell": base_sell,
                "trend": trend, "trend_percent": pct,
                "supply_level": round(supply_factor, 2),
                "name": v["name"]
            }
        return json_resp({"date": time.strftime("%Y-%m-%d"), "season": season,
                          "weather": weather, "crops": prices, "foods": foods})

    # GET /api/market/contracts
    if method == "GET" and p == "/api/market/contracts":
        season, day = get_season_day()
        # Generate contracts for first 3 days of each season
        global _active_contracts, _active_contracts_season
        if not _active_contracts or _active_contracts_season != season:
            _active_contracts = []
            _active_contracts_season = season
            candidates = []
            for k, v in CROPS.items():
                if season in v.get("seasons", []):
                    candidates.append(k)
            if candidates:
                ctypes = random.sample(candidates, min(4, len(candidates)))
                for ct in ctypes:
                    crop = CROPS[ct]
                    base_price = crop["sell"]
                    _active_contracts.append({
                        "id": f"contract_{season}_{ct}",
                        "crop_type": ct,
                        "crop_name": crop["name"],
                        "requested_quantity": random.choice([5, 8, 10, 12, 15]),
                        "locked_price": int(base_price * 1.2),  # +20% above spot
                        "deadline_season": season,
                        "deadline_day": random.choice([18, 20, 22, 25]),
                        "penalty_rate": 0.30,
                        "signed_by": [],
                        "delivered": 0,
                        "expired": False,
                    })
        return json_resp({"success": True, "contracts": _active_contracts})

    # POST /api/market/contracts/{id}/sign
    m = re.match(r'/api/market/contracts/([^/]+)/sign', p)
    if m and method == "POST":
        cid = m.group(1)
        agent_id = parse_auth(headers) or (json.loads(body).get("agent_id", "") if body else "")
        contracts = _active_contracts
        contract = next((c for c in contracts if c["id"] == cid), None)
        if not contract:
            return json_resp({"success": False, "error": "合同不存在"})
        if contract["expired"]:
            return json_resp({"success": False, "error": "合同已过期"})
        if agent_id in contract["signed_by"]:
            return json_resp({"success": False, "error": "已签过此合同"})
        # Price discovery: more signers = lower locked price
        total_signed = contract["delivered"] + len(contract["signed_by"])
        price_mod = max(0.7, 1.0 - total_signed * 0.02)
        contract["locked_price"] = max(1, int(contract["locked_price"] * price_mod / max(0.85, price_mod)))
        contract["signed_by"].append(agent_id)
        # Also store on the farm
        for fid, f in farms.items():
            if f.get("agent_id") == agent_id:
                f.setdefault("signed_contracts", []).append(contract["id"])
                break
        return json_resp({"success": True,
            "action_result": f"签约{contract['crop_name']}合同: {contract['requested_quantity']}颗×{contract['locked_price']}G, 截止D{contract['deadline_day']}"})

    # POST /api/market/contracts/{id}/deliver
    m = re.match(r'/api/market/contracts/([^/]+)/deliver', p)
    if m and method == "POST":
        cid = m.group(1)
        try: data = json.loads(body) if body else {}
        except: data = {}
        agent_id = data.get("agent_id", "")
        contracts = _active_contracts
        contract = next((c for c in contracts if c["id"] == cid), None)
        if not contract:
            return json_resp({"success": False, "error": "合同不存在"})
        if contract["delivered"] >= contract["requested_quantity"]:
            return json_resp({"success": False, "error": "合同已全部交货"})
        # Find the farm
        farm = None
        for fid, f in farms.items():
            if f.get("agent_id") == agent_id:
                farm = f; break
        if not farm:
            return json_resp({"success": False, "error": "农场不存在"})
        # Deliver from storage
        needed = contract["requested_quantity"] - contract["delivered"]
        available = [s for s in farm.get("storage", []) if s["crop_type"] == contract["crop_type"]]
        delivered = 0
        for item in available[:needed]:
            farm["storage"].remove(item)
            delivered += 1
        if delivered == 0:
            return json_resp({"success": False, "error": f"仓库中没有{contract['crop_name']}可以交货"})
        contract["delivered"] += delivered
        gold_earned = delivered * contract["locked_price"]
        farm["gold"] += gold_earned
        return json_resp({"success": True,
            "action_result": f"交{contract['crop_name']}×{delivered}，获得{gold_earned}G（合同价{contract['locked_price']}G/颗）"})

    # GET /api/leaderboard
    if method == "GET" and p == "/api/leaderboard":
        entries = [{"agent_name":f["agent_name"],"gold":f["gold"],"level":1}
                   for f in list(farms.values())[:10]]
        return json_resp({"data":{"entries":entries}})

    # POST /api/farm/register
    if method == "POST" and p == "/api/farm/register":
        try: data = json.loads(body) if body else {}
        except: data = {}
        name = data.get("agent_id","") or data.get("name","farmer")
        fid = str(uuid.uuid4())
        season, day = get_season_day()
        farms[fid] = {
            "farm_id": fid, "agent_name": name, "agent_id": agent_id or name,
            "season": season, "season_en": season, "day": day, "year": 1,
            "weather": "sunny", "gold": 2000, "xp": 0, "farm_level": 1, "score": 0,
            "day_phase": random.choice(DAY_PHASES),
            "gdd_today": SEASON_GDD_BASE.get(season, 10),
            "energy": {"current":200,"max":200},
            "terrain": generate_terrain(),
            "grid_size": {"x": GRID_SIZE_X, "y": GRID_SIZE_Y},
            "crops": [], "soil_history": {},
            "buildings": [], "construction_queue": [],
            "storage": [],
            "saved_seeds": {},
            "wild_events_queue": [],
            "livestock": [],
            "manure_stockpile": 0,
            "hay_stockpile": 0,  # harvested from wheat/corn, fed to cows/sheep
            "farmer": {
                "hunger": 100, "hydration": 100, "fatigue": 0,
                "skills": {"farming":1,"husbandry":1,"machinery":1,"processing":1},
                "xp": {"farming":0,"husbandry":0,"machinery":0,"processing":0},
            },
            "inventory_items": [{"key":"hoe","name":"锄头","count":1},{"key":"watering_can","name":"水壶","count":1}],
            "land_status": {"tilled":0,"planted":0,"watered":0,"empty":0},
            "suggestions": ["体力充足，可开垦土地——morning有5次行动机会"],
            "animals": [],
            "daily_quests": [],
            "day_actions": {"morning": 5, "afternoon": 3, "evening": 1},
            "day_actions_used": 0,
            # ═══ Phase A stubs ═══
            "weather_state": {"dominant":"sunny","hourly_history":["sunny"]*24,
                              "special_notes":[],"frost_warning":False},
            "weeds": [],                 # [{"x":int,"y":int,"density":float}]
            "topsoil_warnings": [],      # populated on status GET
            # ═══ Phase B stubs ═══
            "available_contracts": [],   # filled by market tick on season start
            "signed_contracts": [],      # contracts the agent committed to
            "tool_status": {"hoe": {"tier":"copper","durability":60,"max_durability":60},
                            "watering_can": {"tier":"copper","durability":60,"max_durability":60}},
            "irrigation_method": None,   # None / "flood" / "sprinkler" / "drip"
            "animal_disease_status": [], # [{"animal_id":...,"sick":True,"isolated":False}]
            # ═══ Phase C stubs ═══
            "perennial_crops": [],       # trees + asparagus planted in orchard/farmland
            "intercrop_tiles": [],       # computed: tiles between trees available for annuals
            # ═══ Phase D1: 24-hour time ═══
            "hour": 7.0,  # D5: 24-hour clock, start at 7am
        }
        return json_resp({"success":True,"farm":farms[fid],"farm_id":fid})

    # GET /api/farm/{id}/view — Godot visualization endpoint (precise data)
    m = re.match(r'/api/farm/([^/]+)/view', p)
    if m and method == "GET":
        fid = m.group(1)
        if fid not in farms: return json_resp({"success":False,"error":"农场不存在"}, 404)
        f = farms[fid]
        X = GRID_SIZE_X; YD = GRID_SIZE_Y
        tiles = [[0]*YD for _ in range(X)]
        crop_overlay = [[None]*YD for _ in range(X)]
        for c in f["crops"]:
            x, y = c.get("position_x",0), c.get("position_y",0)
            if 0 <= x < X and 0 <= y < YD:
                pct = min(100, int(c.get("gdd_accumulated",0) / max(1, c.get("gdd_required",1)) * 100))
                crop_overlay[x][y] = {
                    "crop": c["crop_type"], "name": c.get("name",""),
                    "variety": c.get("variety","standard"),
                    "gdd_pct": pct,
                    "watered": c.get("watered_today", False),
                    "health": "good" if not c.get("storm_damaged") and c.get("missed_waterings",0)==0 else "damaged"
                }
        return json_resp({
            "season": f["season"], "day": f["day"], "weather": f["weather"],
            "day_phase": f.get("day_phase","morning"),
            "gold": f["gold"], "score": f.get("score",0),
            "energy": f["energy"]["current"],
            "tiles": tiles, "crop_overlay": crop_overlay,
            "tilled": f["land_status"]["tilled"],
            "buildings": f.get("buildings", []),
            "gdd_today": f.get("gdd_today", 10),
            "terrain_zones": f.get("terrain", {}).get("zones", []),
            "water_sources": f.get("terrain", {}).get("water_sources", []),
        })

    # GET /api/farm/{id}/status
    m = re.match(r'/api/farm/([^/]+)/status', p)
    if m and method == "GET":
        fid = m.group(1)
        if fid not in farms: return json_resp({"success":False,"error":"农场不存在"}, 404)
        f = farms[fid]
        # Phase 2: bounded rationality — natural language crop observations
        observed_crops = []
        for c in f["crops"]:
            stage = c["growth_stage"]; max_s = c["max_growth_stage"]
            ratio = stage / max(max_s, 1)
            watered = c.get("watered_today", False)
            missed = c.get("missed_waterings", 0)
            damaged = c.get("storm_damaged", False)
            # Get stage names from crop config
            ct = c.get("crop_type", "parsnip")
            stage_names = CROPS.get(ct, {}).get("stage_names", ["种子","发芽","幼苗","长叶","开花","成熟"])
            stage_idx = min(len(stage_names)-1, int(ratio * (len(stage_names)-1)))
            # Keep clamped to actual GDD range
            if ratio <= 0.02: stage_idx = 0
            elif ratio >= 0.98: stage_idx = len(stage_names)-1
            else: stage_idx = max(1, min(len(stage_names)-2, int(ratio * len(stage_names))))
            stage_name = stage_names[stage_idx]
            stage_total = len(stage_names)

            if ratio <= 0.05: desc = f"【{stage_name}】刚种下，还没有发芽的迹象"
            elif ratio <= 0.3: desc = f"【{stage_name}】刚刚冒出嫩芽"
            elif ratio <= 0.55: desc = f"【{stage_name}】正在快速生长"
            elif ratio <= 0.75: desc = f"【{stage_name}】长得很茂盛了"
            elif ratio <= 0.90: desc = f"【{stage_name}】快要成熟了"
            elif ratio < 1.0: desc = f"【{stage_name}】已经长满果实，差不多可以收了"
            else: desc = f"【{stage_name}】已经完全成熟，可以收获了！"
            health = []
            if not watered and ratio < 0.9: health.append("土壤有点干")
            if missed >= 2: health.append("叶片边缘发黄")
            if damaged: health.append("有些枝叶折断了")
            if missed >= 3: health.append("看起来蔫蔫的")
            if not health and watered and missed == 0: health.append("看起来非常健康")
            # Weed neighbor penalty (Phase A §4)
            weed_positions = {(w["x"], w["y"]) for w in f.get("weeds", [])}
            neighbor_weeds = 0
            for dx, dy in [(-1,0),(1,0),(0,-1),(0,1)]:
                if (c.get("position_x",0) + dx, c.get("position_y",0) + dy) in weed_positions:
                    neighbor_weeds += 1
            if neighbor_weeds >= 3:
                health.append("周围杂草丛生——养分被抢夺!")
            elif neighbor_weeds >= 1:
                health.append("附近有杂草")
            # pH warning (Phase A §5)
            soil_npk = f.get("terrain", {}).get("soil_npk", [])
            if soil_npk:
                px, py = c.get("position_x", 0), c.get("position_y", 0)
                if px < len(soil_npk) and py < len(soil_npk[0]) and soil_npk[px][py]:
                    soil_ph = soil_npk[px][py].get("pH", 6.5)
                    opt_ph = CROPS.get(c.get("crop_type", ""), {}).get("ph_opt", 6.0)
                    tol = CROPS.get(c.get("crop_type", ""), {}).get("ph_tol", 1.5)
                    dev = abs(soil_ph - opt_ph)
                    if dev > tol * 1.5:
                        health.append(f"土壤pH {soil_ph}严重偏离——产量x0.3!")
                    elif dev > tol:
                        health.append(f"土壤pH {soil_ph}偏{'碱' if soil_ph > opt_ph else '酸'}x0.6")
            # Pollination status (Phase B §6)
            ct = c.get("crop_type", "")
            if ct in INSECT_POLLINATED:
                poll = pollination_rate(f, c.get("position_x", 0), c.get("position_y", 0))
                if poll >= 1.0:
                    health.append("🐝 传粉充足")
                elif poll >= 0.5:
                    health.append("⚠ 传粉不足×0.7")
                else:
                    health.append("⚠ 严重缺传粉×0.3——建蜂箱!")
            gdd_pct = min(100, int(c.get("gdd_accumulated",0) / max(1, c.get("gdd_required",1)) * 100))
            observed_crops.append({
                "position": f"第{c['position_x']}行第{c['position_y']}列",
                "crop_name": c.get("name", c.get("crop_type", "作物")),
                "description": desc, "health": health,
                "_stage_hint": stage, "_max_hint": max_s,
                "gdd_percent": gdd_pct, "gdd_accumulated": c.get("gdd_accumulated",0),
                "gdd_required": c.get("gdd_required",99),
                "watered_today": c.get("watered_today", False),
                "position_x": c.get("position_x", 0), "position_y": c.get("position_y", 0),
            })
        return json_resp({"data":{
            "season":f["season"],"season_en":f["season_en"],
            "day":f["day"],"year":f["year"],"weather":f["weather"],
            "day_phase": f.get("day_phase", "morning"),
            "day_actions_used": f.get("day_actions_used", 0),
            "day_actions": f.get("day_actions", {"morning":5,"afternoon":3,"evening":1}),
            "hour": f.get("hour", 7.0),  # D5: current clock hour (0-24)
            "season_daylight": DAY_RANGE.get(f["season"], (6,20)),   # D5: (sunrise, sunset)
            "is_night": not is_daytime(f),  # D5: based on actual clock hour
            "gdd_today": f.get("gdd_today", 10),
            "gold":f["gold"],"xp":f["xp"],"farm_level":f["farm_level"],
            "score": f.get("score", 0),
            "energy":{"current":f["energy"]["current"],"max":f["energy"]["max"]},
            "crops": observed_crops, "inventory_items":f["inventory_items"],
            "land_status":f["land_status"],
            "soil_history": f.get("soil_history", {}),
            "suggestions": _suggestions(f),
            "sensory_observations": sensory_report(f),  # Phase E1: NL sensory data
            "animals":f["animals"],
            "water_sources": f.get("terrain", {}).get("water_sources", []),
            "buildings": f.get("buildings", []),
            "building_options": {k: {"name":v["name"],"cost":v["cost"],"desc":v["desc"]} for k,v in BUILDINGS.items() if k not in f.get("buildings", [])},
            "saved_seeds": f.get("saved_seeds", {}),
            "storage": f.get("storage", []),
            "storage_capacity": (50 + (100 if "root_cellar" in f.get("buildings",[]) else 0)
                                 + (200 if "silo" in f.get("buildings",[]) else 0)),
            "storage_summary": _storage_summary(f),
            "npk_summary": _npk_summary(f),
            "farmer": f.get("farmer", {}),
            "livestock": f.get("livestock", []),
            "manure_stockpile": f.get("manure_stockpile", 0),
            "daily_quests":f["daily_quests"],
            # Phase A fields
            "weather_notes": f.get("weather_state",{}).get("special_notes",[]),
            "frost_warning": f.get("weather_state",{}).get("frost_warning",False),
            "weed_count": len(f.get("weeds",[])),
            "topsoil_warnings": f.get("topsoil_warnings",[]),
            "avg_topsoil": (sum(sum(row) for row in f.get("terrain",{}).get("topsoil_depth",[[15]*28]*20))
                            / max(1, GRID_SIZE_X*GRID_SIZE_Y)),
            # Phase B fields
            "available_contracts": f.get("available_contracts", []),
            "signed_contracts": f.get("signed_contracts", []),
            "tool_status": f.get("tool_status", {}),
            "irrigation_status": {"method": f.get("irrigation_method"),
                                  "sprinkler": "sprinkler" in f.get("buildings",[]),
                                  "drip": "drip" in f.get("buildings",[])},
            "animal_disease_status": f.get("animal_disease_status", []),
            # Phase C fields
            "perennial_crops": f.get("perennial_crops", []),
            "intercrop_tiles": f.get("intercrop_tiles", []),
            # Phase B: include irrigation buildings in building_options
            "irrigation_options": {k: {"name":v["name"],"cost":v["cost"],"desc":v["desc"]}
                                   for k,v in IRRIGATION_BUILDINGS.items()
                                   if k not in f.get("buildings",[])},
        }})

    # POST /api/farm/{id}/action
    m = re.match(r'/api/farm/([^/]+)/action', p)
    if m and method == "POST":
        fid = m.group(1)
        if fid not in farms: return json_resp({"success":False,"error":"农场不存在"}, 404)
        try: data = json.loads(body) if body else {}
        except: data = {}
        action = data.get("action_type","")
        f = farms[fid]

        season = f["season"]

        # ─── farmer init ───
        farmer = f.setdefault("farmer", {"hunger":100,"hydration":100,"fatigue":0,
            "skills":{"farming":1,"husbandry":1,"machinery":1,"processing":1},
            "xp":{"farming":0,"husbandry":0,"machinery":0,"processing":0},
            "fitness":1.0, "knowledge":{"farming":0,"husbandry":0,"economics":0,"machinery":0},"sleepiness":0})

        # ═══ D5: Auto day-advance if clock crossed midnight ═══
        if f.setdefault("hour", 7.0) >= 24.0 and action != "sleep":
            _do_day_advance(f)
            f["hour"] = f["hour"] - 24.0
            # Reset daily flags
            for c in f.get("crops", []):
                c["watered_today"] = False
            _tick_weeds(f)
            tick_animal_disease(f)
            tick_perennials(f)

        # ═══ TRUE 24-HOUR CLOCK (Phase D5) ═══
        hour = f.setdefault("hour", 7.0)
        tcost = TIME_COST.get(action, 0.5)
        # D2: fitness reduces physical time costs
        fit = farmer.get("fitness", 1.0)
        if action in ("till","till_bulk","plant","plant_bulk","water","harvest",
                       "weed_all","build","fell_tree","prune","mulch","plant_tree",
                       "fertilize","green_manure","spread_manure","lime","sulfur",
                       "irrigate_flood","repair","forge","slaughter","bury"):
            tcost *= fitness_time_mult(fit)
        # D2: per-animal feeding time
        if action == "feed_animals" or action == "water_animals" or action == "collect_products":
            animals = f.get("livestock", [])
            tcost = 0
            atype = {"feed_animals":"feed","water_animals":"water","collect_products":"collect"}[action]
            for a in animals:
                sp = a.get("species","chicken")
                tcost += ANIMAL_TIME_COST.get(sp, {}).get(atype, 0.1)
            tcost = max(0.1, tcost)  # at least 0.1h
        # Night restriction (based on actual clock time, not remaining daylight)
        if not is_daytime(f) and action in NIGHT_BLOCKED:
            sr, ss = DAY_RANGE.get(season, (6, 20))
            return json_resp({"success": False,
                "action_result": f"天黑了——现在是{hour:.0f}:00，'{action}'只能在白天{sr:.0f}-{ss:.0f}点做!"})
        # Clock advance: most actions advance the clock
        if action not in ("next_day","sleep","exercise","read","lookup","drink_water","drink_coffee","remember","recall","forget") and tcost > 0:
            f["hour"] = hour + tcost
            tick_hourly_gdd(f, tcost)  # D5: crops grow in real time

        # ═══ FATIGUE & SLEEPINESS CHECK ═══
        if action not in ("next_day","sleep","eat","drink_water"):
            # Sleepiness force-sleep
            slp = farmer.get("sleepiness", 0)
            if slp >= SLEEPINESS_FORCE_SLEEP:
                return json_resp({"success":False,
                    "action_result":f"困到极限了(sleepiness={slp})——必须先 sleep 才能做其他事!"})
            # Sleepiness mistake probability with varied failure messages
            if slp > SLEEPINESS_MISTAKE_HIGH and random.random() < 0.05:
                return json_resp({"success":False,
                    "action_result":random.choice(SLEEPINESS_MISTAKES)})
            if slp > SLEEPINESS_MISTAKE_LOW and random.random() < 0.02:
                return json_resp({"success":False,
                    "action_result":random.choice(SLEEPINESS_MISTAKES)})
            # Accumulate sleepiness: faster at night
            is_night = not is_daytime(f)
            rate = SLEEPINESS_NIGHT_RATE if is_night else SLEEPINESS_DAY_RATE
            farmer["sleepiness"] = min(SLEEPINESS_MAX, slp + rate * tcost)
            if farmer["fatigue"] >= 100:
                return json_resp({"success":False,
                    "action_result":"体力耗尽(fatigue=100)——只能 sleep！"})
            if farmer["hunger"] < 5 and action in ("till","build","slaughter"):
                return json_resp({"success":False,
                    "action_result":"太饿了——先 eat 吃东西再干重活。"})

        # ═══ ENERGY DEDUCTION ═══
        ecost = ENERGY_COST.get(action, 5)
        if "tool_shed" in f.get("buildings", []):
            ecost = int(ecost * 0.7)
        # Physical fitness bonus (higher fitness = less energy cost)
        fitness = farmer.get("fitness", 1.0)
        if action not in ("exercise","read","sleep","eat","drink_water","next_day","lookup"):
            ecost = max(1, int(ecost * max(0.6, 1.2 - fitness * 0.1)))
        # Phase E3: Tool bonuses
        tools = f.get("tool_status", {})
        if action == "harvest" and "sickle" in tools:
            tier_bonus = {"iron": 0.8, "steel": 0.7, "iridium": 0.6}.get(tools["sickle"].get("tier","copper"), 1.0)
            ecost = int(ecost * tier_bonus)
        if action == "drain" and "spade" in tools:
            tier_bonus = {"iron": 0.85, "steel": 0.7, "iridium": 0.55}.get(tools["spade"].get("tier","copper"), 1.0)
            ecost = int(ecost * tier_bonus)
        # Hunger/fatigue multipliers
        mult = 1.0
        if farmer["hunger"] < 20: mult += 0.5
        elif farmer["hunger"] < 5: mult += 0.5
        if farmer["hydration"] < 20: mult += 0.3
        if farmer["fatigue"] > 80: mult += 0.5
        ecost = int(ecost * mult)
        f["energy"]["current"] = max(0, f["energy"]["current"] - ecost)
        # ═══ SKILL XP ═══
        xp_entry = SKILL_XP.get(action)
        if xp_entry:
            sk, xp = xp_entry
            farmer["xp"][sk] = farmer["xp"].get(sk, 0) + xp
            # Level up check
            xp_total = farmer["xp"][sk]
            lvl = farmer["skills"][sk]
            thresholds = {1:100, 2:250, 3:500, 4:1000}
            if lvl < 5 and xp_total >= thresholds.get(lvl, 99999):
                farmer["skills"][sk] = lvl + 1
                f["score"] = f.get("score", 0) + 30

        # ═══ BULK ACTIONS (farming Lv2+) ═══
        elif action == "till_bulk":
            farm_skill = farmer.get("skills",{}).get("farming",1)
            if farm_skill < 2:
                return json_resp({"success":False,"action_result":"农耕技能不足(Lv2+才能批量开垦)"})
            count = data.get("count", data.get("quantity", 3))
            count = min(count, 9)  # max 9 at once
            total_energy = int(count * 20 * 0.8)  # 80% of individual cost
            if f["energy"]["current"] < total_energy:
                return json_resp({"success":False,"action_result":f"体力不足——批量开垦{count}块需要{total_energy}体力"})
            f["energy"]["current"] = max(0, f["energy"]["current"] - total_energy)
            f["land_status"]["tilled"] += count
            f["land_status"]["empty"] += count
            f["xp"] += 5 * count
            # Bulk work penalty: fatigue + hunger + thirst
            farmer["fatigue"] = min(100, farmer.get("fatigue",0) + int(count * 1.5))
            farmer["hunger"] = max(0, farmer.get("hunger",100) - int(count * 0.8))
            farmer["hydration"] = max(0, farmer.get("hydration",100) - int(count * 0.6))
            # Skill XP
            xp_entry = SKILL_XP.get("till_bulk")
            if xp_entry:
                sk, xp = xp_entry
                farmer["xp"][sk] = farmer["xp"].get(sk, 0) + xp
            return json_resp({"success":True,
                "action_result":f"批量开垦{count}块地 (体力-{total_energy} 疲惫+{int(count*1.5)} 饥饿-{int(count*0.8)} 口渴-{int(count*0.6)})",
                "state_changes":{"energy":-total_energy,"xp":5*count}})

        elif action == "plant_bulk":
            farm_skill = farmer.get("skills",{}).get("farming",1)
            if farm_skill < 2:
                return json_resp({"success":False,"action_result":"农耕技能不足(Lv2+才能批量种植)"})
            ct = (data.get("crop_type","") or data.get("item","") or data.get("seed","") or "parsnip").replace("_seeds","").strip()
            if ct not in CROPS: return json_resp({"success":False,"action_result":"未知作物"})
            if season not in CROPS[ct]["seasons"] and "greenhouse" not in f.get("buildings", []):
                return json_resp({"success":False,"action_result":f"错误：{CROPS[ct]['name']}不能在{season}种植"})
            count = data.get("count", data.get("quantity", 3))
            count = min(count, 9, f["land_status"]["empty"])
            seed_key = f"{ct}_seeds"
            found = False
            for item in f["inventory_items"]:
                if item["key"] == seed_key and item["count"] >= count:
                    item["count"] -= count; found = True; break
            if not found:
                return json_resp({"success":False,"action_result":f"种子不足——需要{count}颗{ct}种子"})
            total_energy = int(count * 12 * 0.8)
            if f["energy"]["current"] < total_energy:
                return json_resp({"success":False,"action_result":f"体力不足——批量种植{count}颗需要{total_energy}体力"})
            f["energy"]["current"] = max(0, f["energy"]["current"] - total_energy)
            gdd_req = CROPS[ct].get("gdd_req", 30)
            for i in range(count):
                f["crops"].append({
                    "crop_type":ct,"name":CROPS[ct]["name"],
                    "variety":"standard","position_x":i%3,"position_y":i//3,
                    "growth_stage":0,"max_growth_stage":4,
                    "gdd_accumulated":0,"gdd_required":gdd_req,
                    "sell_price":CROPS[ct].get("sell",50),
                    "watered_today":False, "missed_waterings":0, "storm_damaged":False,
                })
            f["land_status"]["planted"] += count
            f["land_status"]["empty"] -= count
            f["xp"] += 5 * count
            farmer["fatigue"] = min(100, farmer.get("fatigue",0) + int(count * 1.2))
            farmer["hunger"] = max(0, farmer.get("hunger",100) - int(count * 0.6))
            farmer["hydration"] = max(0, farmer.get("hydration",100) - int(count * 0.5))
            xp_entry = SKILL_XP.get("plant_bulk")
            if xp_entry:
                sk, xp = xp_entry
                farmer["xp"][sk] = farmer["xp"].get(sk, 0) + xp
            return json_resp({"success":True,
                "action_result":f"批量种植{count}颗{CROPS[ct]['name']} (体力-{total_energy} 疲惫+{int(count*1.2)} 饥饿-{int(count*0.6)} 口渴-{int(count*0.5)})",
                "state_changes":{"energy":-total_energy,"xp":5*count}})

        if action == "till":
            positions = data.get("positions",[[0,0]])
            n = len(positions)
            f["land_status"]["tilled"] += n
            f["land_status"]["empty"] += n
            f["xp"] += 5*n
            return json_resp({"success":True,"action_result":f"成功开垦{n}块地",
                              "state_changes":{"energy":-ecost,"xp":5*n}})

        elif action == "plant":
            ct = (data.get("crop_type","") or data.get("item","") or data.get("seed","") or data.get("crop","") or "parsnip").replace("_seeds","").strip()
            if ct not in CROPS: return json_resp({"success":False,"action_result":"未知作物"})
            if season not in CROPS[ct]["seasons"] and "greenhouse" not in f.get("buildings", []):
                return json_resp({"success":False,"action_result":f"错误：{CROPS[ct]['name']}不能在{season}种植"})
            positions = data.get("positions",[[0,0]])
            n = min(len(positions), f["land_status"]["empty"])
            seed_key = f"{ct}_seeds"
            found = False
            for item in f["inventory_items"]:
                if item["key"] == seed_key and item["count"] >= n:
                    item["count"] -= n; found = True; break
            if not found:
                return json_resp({"success":False,"action_result":f"种子不足"})
            # Support variety selection
            variety = data.get("variety","standard")
            if variety not in CROPS[ct].get("varieties",{}):
                variety = "standard"  # fallback
            gdd_req = CROPS[ct]["varieties"][variety]["gdd"]
            sell_price = CROPS[ct]["varieties"][variety]["sell"]
            for i in range(n):
                x,y = positions[i] if i < len(positions) else (0,i)
                pk = f"{x},{y}"
                if "soil_history" not in f: f["soil_history"] = {}
                if pk not in f["soil_history"]: f["soil_history"][pk] = []
                f["soil_history"][pk].append(ct)
                if len(f["soil_history"][pk]) > 6: f["soil_history"][pk] = f["soil_history"][pk][-6:]
                f["crops"].append({
                    "crop_type":ct,"name":CROPS[ct]["name"],
                    "variety":variety,"position_x":x,"position_y":y,
                    "growth_stage":0,"max_growth_stage":4,
                    "gdd_accumulated":0,"gdd_required":gdd_req,
                    "sell_price":sell_price,
                    "watered_today":False, "missed_waterings":0, "storm_damaged":False,
                })
            f["land_status"]["planted"] += n
            f["land_status"]["empty"] -= n
            return json_resp({"success":True,"action_result":f"成功种植{n}颗{CROPS[ct]['name']}({CROPS[ct]['varieties'][variety].get('traits','?')})",
                              "state_changes":{"energy":-ecost,"xp":5*n}})

        elif action == "water":
            weather = f.get("weather","sunny")
            moisture_grid = f.get("terrain", {}).get("soil_moisture", [])
            positions = data.get("positions", [])
            # If no positions given, water all crops (backward compat)
            if not positions:
                positions = [[c.get("position_x",0), c.get("position_y",0)] for c in f["crops"]]
            # Phase B: tool splash (steel=radius 1, iridium=radius 2)
            tool_tier = f.get("tool_status", {}).get("watering_can", {}).get("tier", "copper")
            splash_range = {"copper": 0, "iron": 0, "steel": 1, "iridium": 2}.get(tool_tier, 0)
            if splash_range > 0:
                expanded = set()
                for x, y in positions:
                    expanded.add((x, y))
                    for dx in range(-splash_range, splash_range + 1):
                        for dy in range(-splash_range, splash_range + 1):
                            if abs(dx) + abs(dy) <= splash_range:
                                expanded.add((x + dx, y + dy))
                positions = [[x, y] for x, y in expanded]
            n_watered = 0
            for pos in positions:
                px, py = pos[0], pos[1]
                if moisture_grid and px < len(moisture_grid) and py < len(moisture_grid[0]):
                    moisture_grid[px][py] = min(100, moisture_grid[px][py] + 40)
                    n_watered += 1
            # Mark crops at these positions as watered_today
            for c in f["crops"]:
                cx, cy = c.get("position_x",0), c.get("position_y",0)
                if [cx, cy] in positions:
                    c["watered_today"] = True
            n = sum(1 for c in f["crops"] if c["watered_today"])
            if weather == "rainy":
                f["score"] = f.get("score",0) - 10
                msg = f"雨天浇灌了{n_watered}块地——过度浇灌！[-10分]"
            elif weather == "drought":
                f["land_status"]["watered"] = n
                f["score"] = f.get("score",0) + 5
                msg = f"旱日浇灌了{n_watered}块地——及时救援！[+5分]"
            else:
                f["land_status"]["watered"] = n
                f["score"] = f.get("score",0) + 2
                msg = f"浇灌了{n_watered}块地 [+2分]"
            return json_resp({"success":True,"action_result":msg,
                              "state_changes":{"score":f.get("score",0)}})

        # ═══ PHASE E3: DRAIN (remove excess water) ═══
        elif action == "drain":
            moisture_grid = f.get("terrain", {}).get("soil_moisture", [])
            positions = data.get("positions", [])
            if not positions:
                positions = [[c.get("position_x",0), c.get("position_y",0)] for c in f["crops"]]
            n_drained = 0
            for pos in positions:
                px, py = pos[0], pos[1]
                if moisture_grid and px < len(moisture_grid) and py < len(moisture_grid[0]):
                    moisture_grid[px][py] = max(0, moisture_grid[px][py] - 30)
                    n_drained += 1
            # Remove water damage from crops at these positions
            for c in f["crops"]:
                if [c.get("position_x",0), c.get("position_y",0)] in positions:
                    c["watered_today"] = False  # dry them out
            if f.get("weather","") in ("stormy","flood"):
                f["score"] = f.get("score",0) + 10
                msg = f"紧急排水{n_drained}块地——防止涝害！[+10分]"
            else:
                f["score"] = f.get("score",0) + 2
                msg = f"排水{n_drained}块地——土壤水分降低30% [+2分]"
            return json_resp({"success":True,"action_result":msg,
                              "state_changes":{"score":f.get("score",0)}})

        elif action == "harvest":
            mature = [c for c in f["crops"]
                      if c.get("gdd_accumulated", 0) >= c.get("gdd_required", 99)]
            if not mature:
                return json_resp({"success":False,"action_result":"没有可收获的作物"})
            total_value = 0; quality_counts = {"S":0,"A":0,"B":0,"C":0}; multi_count = 0
            total_fruits = 0; total_seeds = 0

            # Pollination bonus from bees
            bee_bonus = 0
            if "beehive" in f.get("buildings", []):
                bees = sum(1 for a in f.get("livestock",[]) if a["species"] == "bee" and a["health"] >= 50)
                bee_bonus = 0.03 * bees  # 3% per hive
            # Farmer skill bonus
            farmer = f.get("farmer", {})
            farm_skill = farmer.get("skills",{}).get("farming", 1)
            skill_bonus = (farm_skill - 1) * 0.02  # 2% per level above 1

            cap = 50
            if "root_cellar" in f.get("buildings", []): cap += 100
            if "silo" in f.get("buildings", []): cap += 200

            for c in mature:
                bp = c.get("sell_price", CROPS[c["crop_type"]]["sell"]); qm = 1.0
                ct = c["crop_type"]; variety = c.get("variety","standard")
                pk = f"{c.get('position_x',0)},{c.get('position_y',0)}"
                sh = f.get("soil_history",{}).get(pk,[])
                cons = 0
                for p in reversed(sh[:-1]):
                    if p == ct: cons += 1
                    else: break
                if cons >= 2: qm *= 0.6
                elif cons >= 1: qm *= 0.85
                mw = c.get("missed_waterings",0)
                if mw >= 3: qm *= 0.5
                elif mw >= 2: qm *= 0.7
                elif mw >= 1: qm *= 0.85
                if c.get("storm_damaged",False): qm *= 0.7
                if len(sh) >= 2 and sh[-2] != ct: qm *= 1.15

                # ── QUALITY SYSTEM (Phase A §6) ──
                # Add Phase A factors to qm chain
                soil_npk = f.get("terrain",{}).get("soil_npk",[])
                snpk = None
                if soil_npk and c.get("position_x",0) < len(soil_npk) and c.get("position_y",0) < len(soil_npk[0]):
                    snpk = soil_npk[c["position_x"]][c["position_y"]]
                if snpk:
                    # pH modifier
                    soil_ph = snpk.get("pH", 6.5)
                    crop_def = CROPS.get(ct, {})
                    dev = abs(soil_ph - crop_def.get("ph_opt", 6.0))
                    tol = crop_def.get("ph_tol", 1.5)
                    if dev > tol * 1.5: qm *= 0.3
                    elif dev > tol: qm *= 0.6
                    elif dev <= tol * 0.5: qm *= 1.0
                    else: qm *= 0.85
                    # Topsoil modifier
                    ts = f.get("terrain",{}).get("topsoil_depth",[[20]*GRID_SIZE_Y]*GRID_SIZE_X)
                    ts_val = ts[c.get("position_x",0)][c.get("position_y",0)] if c.get("position_x",0) < len(ts) and c.get("position_y",0) < len(ts[0]) else 20
                    if ts_val < 5: qm *= 0.7
                    # Organic matter modifier
                    om = snpk.get("organic_matter", 10)
                    if om < 3: qm *= 0.6

                # Phase B: Pollination modifier (insect-pollinated crops only)
                if ct in INSECT_POLLINATED:
                    poll = pollination_rate(f, c.get("position_x", 0), c.get("position_y", 0))
                    qm *= pollination_yield_mod(poll)

                # Genetic bonus from seed quality
                genetic_bonus = CROPS.get(ct, {}).get("genetic_bonus", 0.0)
                qm = max(0.2, min(2.0, qm + genetic_bonus))

                # Probability distribution → quality tier
                quality = "B"
                for q_min, q_max, s_p, a_p, b_p, c_p in QUALITY_DISTRIBUTION:
                    if q_min <= qm <= q_max:
                        roll = random.random()
                        if roll < s_p: quality = "S"
                        elif roll < s_p + a_p: quality = "A"
                        elif roll < s_p + a_p + b_p: quality = "B"
                        else: quality = "C"
                        break

                # Soil health caps (apply after probability roll)
                if snpk:
                    om_val = snpk.get("organic_matter", 10)
                    q_order = {"C":0,"B":1,"A":2,"S":3}
                    if om_val < 3:
                        quality = "C"  # OM below 3 → C-locked regardless of roll
                    elif om_val < 5 and q_order.get(quality,1) > 1:
                        quality = "B"
                    dev = abs(snpk.get("pH",6.5) - CROPS.get(ct,{}).get("ph_opt",6.0))
                    tol = CROPS.get(ct,{}).get("ph_tol",1.5)
                    if dev > tol * 1.5 and q_order.get(quality,1) > 1:
                        quality = "B"
                    elif dev > tol * 1.2 and q_order.get(quality,1) > 2:
                        quality = "A"
                    ts_val = f.get("terrain",{}).get("topsoil_depth",[[20]*GRID_SIZE_Y]*GRID_SIZE_X)
                    ts_val = ts_val[c.get("position_x",0)][c.get("position_y",0)] if c.get("position_x",0) < len(ts_val) and c.get("position_y",0) < len(ts_val[0]) else 20
                    if ts_val < 5 and q_order.get(quality,1) > 1:
                        quality = "B"
                    elif ts_val < 8 and q_order.get(quality,1) > 2:
                        quality = "A"

                # Quality multiplier effects (price, freshness, genetic)
                qm_data = QUALITY_MULTIPLIERS.get(quality, QUALITY_MULTIPLIERS["B"])
                price_mult = qm_data["price"]
                freshness_bonus = qm_data["freshness_bonus"]

                # Fruit count: bee/skill bonus affects quantity, not quality
                n_fruits = 4 if quality == "S" else (3 if quality == "A" else 2)
                n_fruits = max(1, n_fruits - 1) if quality == "C" else n_fruits
                # Bee + skill bonus: chance of +1 fruit
                if random.random() < bee_bonus + skill_bonus:
                    n_fruits += 1
                n_seeds = random.randint(1, 4) if quality == "S" else (random.randint(0, 3) if quality == "A" else random.randint(0, 2))

                quality_counts[quality] += 1
                sp = max(1, int(bp * price_mult))
                fd = int(CROPS.get(ct, {}).get("freshness_days", 7) * (1.0 + freshness_bonus))
                total_value += sp * n_fruits
                total_fruits += n_fruits; total_seeds += n_seeds

                # Store fruits
                current_stored = len(f.get("storage", []))
                for _ in range(n_fruits):
                    if current_stored + 1 > cap:
                        f["gold"] += max(1, int(sp * 0.6))  # overflow auto-sell
                    else:
                        f["storage"].append({
                            "id": str(uuid.uuid4())[:8],
                            "crop_type": ct, "variety": variety,
                            "name": CROPS.get(ct,{}).get("name", ct),
                            "quality": quality, "sell_price": sp,
                            "freshness_days": fd, "max_freshness": fd,
                            "harvest_day": f["day"], "harvest_season": f["season"],
                            "processed": False,
                        })
                        current_stored += 1

                # Recover seeds → inventory
                if n_seeds > 0:
                    seed_key = f"{ct}_seeds"
                    for item in f.get("inventory_items", []):
                        if item["key"] == seed_key:
                            item["count"] += n_seeds; break
                    else:
                        f["inventory_items"].append({
                            "key": seed_key,
                            "name": f"{CROPS.get(ct,{}).get('name',ct)}种子",
                            "count": n_seeds})

                # Straw production for grain crops (Phase A → Phase B livestock feed)
                crop_def = CROPS.get(ct, {})
                if crop_def.get("produces_straw", False):
                    f["hay_stockpile"] = f.get("hay_stockpile", 0) + n_fruits * 2

                # Multi-harvest crops regrow
                if crop_def.get("multi_harvest", False):
                    c["gdd_accumulated"] = int(c.get("gdd_required", 99) * 0.25)
                    c["growth_stage"] = 1
                    c["missed_waterings"] = 0
                    c["storm_damaged"] = False
                    multi_count += 1
                else:
                    f["crops"].remove(c)

            # NPK depletion
            soil_npk_grid = f.get("terrain", {}).get("soil_npk", [])
            for c in mature:
                ct2 = c["crop_type"]; px2 = c.get("position_x",0); py2 = c.get("position_y",0)
                npk_use = CROP_NPK.get(ct2, {"N":-5,"P":-3,"K":-5})
                if soil_npk_grid and px2 < len(soil_npk_grid) and py2 < len(soil_npk_grid[0]):
                    soil = soil_npk_grid[px2][py2]
                    if soil:
                        soil["N"] = max(0, soil["N"] + npk_use["N"])
                        soil["P"] = max(0, soil["P"] + npk_use["P"])
                        soil["K"] = max(0, soil["K"] + npk_use["K"])
                        soil["organic_matter"] = min(25, soil["organic_matter"] + 1)

            f["land_status"]["planted"] -= (len(mature) - multi_count)
            f["land_status"]["empty"] += (len(mature) - multi_count)
            f["score"] = f.get("score",0) + 10 + total_fruits

            tier_msg = ""
            if quality_counts.get("S",0) > 0: tier_msg += f"⚡S级×{quality_counts['S']}! "
            q_str = ", ".join(f"{g}级×{n}" for g,n in quality_counts.items() if n > 0)
            return json_resp({"success":True,
                "action_result": f"Harvest {len(mature)} plants -> {total_fruits} fruits + {total_seeds} seeds [{q_str}] value~{total_value}G{tier_msg}",
                "state_changes":{"fruits": total_fruits, "seeds": total_seeds,
                    "score": 10 + total_fruits, "storage_count": len(f.get("storage",[]))}})

        elif action == "sign_contract":
            contract_id = data.get("contract_id", "")
            contracts = _active_contracts
            contract = next((c for c in contracts if c["id"] == contract_id), None)
            if not contract:
                return json_resp({"success": False, "action_result": "合同不存在"})
            if contract["expired"]:
                return json_resp({"success": False, "action_result": "合同已过期"})
            if agent_id in contract.get("signed_by", []):
                return json_resp({"success": False, "action_result": "已签过此合同"})
            total_signed = contract.get("delivered", 0) + len(contract.get("signed_by", []))
            price_mod = max(0.7, 1.0 - total_signed * 0.02)
            contract.setdefault("signed_by", []).append(agent_id)
            f.setdefault("signed_contracts", []).append(contract["id"])
            f.setdefault("available_contracts", []).append(contract)
            return json_resp({"success": True,
                "action_result": f"签约{contract['crop_name']}合同: {contract['requested_quantity']}颗×{contract['locked_price']}G"})

        elif action == "deliver_contract":
            contract_id = data.get("contract_id", "")
            contracts = _active_contracts
            contract = next((c for c in contracts if c["id"] == contract_id), None)
            if not contract:
                return json_resp({"success": False, "action_result": "合同不存在"})
            if contract["delivered"] >= contract["requested_quantity"]:
                return json_resp({"success": False, "action_result": "合同已全部交货"})
            needed = contract["requested_quantity"] - contract["delivered"]
            available = [s for s in f.get("storage", []) if s["crop_type"] == contract["crop_type"]]
            delivered = 0
            for item in available[:needed]:
                f["storage"].remove(item)
                delivered += 1
            if delivered == 0:
                return json_resp({"success": False, "action_result": f"仓库中没有{contract['crop_name']}可以交货"})
            contract["delivered"] += delivered
            gold_earned = delivered * contract["locked_price"]
            f["gold"] += gold_earned
            return json_resp({"success": True,
                "action_result": f"交{contract['crop_name']}×{delivered}，获得{gold_earned}G"})

        elif action == "buy":
            crop_type = data.get("crop_type","") or data.get("item_type","") or data.get("item","")
            qty = data.get("quantity",1)
            # Check food shop first
            if crop_type in MARKET_FOODS:
                fd = MARKET_FOODS[crop_type]
                cost = fd["price"] * qty
                if f["gold"] < cost:
                    return json_resp({"success":False,"action_result":"金币不足"})
                f["gold"] -= cost
                for _ in range(qty):
                    f["storage"].append({
                        "id": str(uuid.uuid4())[:8],"crop_type":crop_type,"variety":"food",
                        "name": fd["name"],"quality":"A","sell_price": fd["price"],
                        "freshness_days": fd["freshness"],"max_freshness": fd["freshness"],
                        "harvest_day": f["day"],"harvest_season": f["season"],
                        "processed": True, "is_food": True,
                    })
                return json_resp({"success":True,
                    "action_result":f"购买了{qty}个{fd['name']}，花费{cost}G——直接存入仓库可食用",
                    "state_changes":{"gold":-cost}})
            # Check CROPS for seeds
            if crop_type not in CROPS:
                return json_resp({"success":False,"action_result":"未知商品"})
            cost = CROPS[crop_type]["buy"] * qty
            if f["gold"] < cost:
                return json_resp({"success":False,"action_result":"金币不足"})
            f["gold"] -= cost
            seed_key = f"{crop_type}_seeds"
            for item in f["inventory_items"]:
                if item["key"] == seed_key:
                    item["count"] += qty; break
            else:
                f["inventory_items"].append({"key":seed_key,"name":f"{CROPS[crop_type]['name']}种子","count":qty})
            return json_resp({"success":True,"action_result":f"购买{qty}个{CROPS[crop_type]['name']}种子，花费{cost}G",
                              "state_changes":{"gold":-cost}})

        elif action == "weed_all":
            weeds = f.setdefault("weeds", [])
            if not weeds:
                return json_resp({"success": False, "action_result": "没有杂草需要清除"})
            if not is_daytime(f) and farmer.get("fatigue", 0) > 80:
                return json_resp({"success": False,
                    "action_result": "太疲劳了——黄昏除草风险太高，先 sleep!"})
            n_weeded = len(weeds)
            om_gain = sum(random.randint(2, 3) for _ in range(n_weeded))
            soil_npk_grid = f.get("terrain", {}).get("soil_npk", [])
            for w in weeds:
                wx, wy = w["x"], w["y"]
                if soil_npk_grid and wx < len(soil_npk_grid) and wy < len(soil_npk_grid[0]):
                    sn = soil_npk_grid[wx][wy]
                    if sn:
                        sn["organic_matter"] = min(25, sn.get("organic_matter", 10) + random.randint(2, 3))
            f["weeds"] = []
            return json_resp({"success": True,
                "action_result": f"清除了{n_weeded}块地的杂草，共增加{om_gain}有机质",
                "state_changes": {"energy": -ecost, "weeds_removed": n_weeded}})

        elif action == "lime":
            positions = data.get("positions", [])
            if not positions:
                return json_resp({"success": False, "action_result": "需要指定要改良的地块位置"})
            n = len(positions)
            cost = n * 15
            if f["gold"] < cost:
                return json_resp({"success": False, "action_result": f"金币不足——需要{cost}G"})
            f["gold"] -= cost
            soil_npk_grid = f.get("terrain", {}).get("soil_npk", [])
            for pos in positions:
                px, py = pos[0], pos[1]
                if soil_npk_grid and px < len(soil_npk_grid) and py < len(soil_npk_grid[0]):
                    sn = soil_npk_grid[px][py]
                    if sn:
                        sn["pH"] = min(8.0, sn.get("pH", 6.5) + 0.5)
            return json_resp({"success": True,
                "action_result": f"施石灰{n}块地，pH提升+0.5，花费{cost}G",
                "state_changes": {"gold": -cost}})

        elif action == "sulfur":
            positions = data.get("positions", [])
            if not positions:
                return json_resp({"success": False, "action_result": "需要指定要改良的地块位置"})
            n = len(positions)
            cost = n * 10
            if f["gold"] < cost:
                return json_resp({"success": False, "action_result": f"金币不足——需要{cost}G"})
            f["gold"] -= cost
            soil_npk_grid = f.get("terrain", {}).get("soil_npk", [])
            for pos in positions:
                px, py = pos[0], pos[1]
                if soil_npk_grid and px < len(soil_npk_grid) and py < len(soil_npk_grid[0]):
                    sn = soil_npk_grid[px][py]
                    if sn:
                        sn["pH"] = max(4.0, sn.get("pH", 6.5) - 0.5)
            return json_resp({"success": True,
                "action_result": f"施硫磺{n}块地，pH降低-0.5，花费{cost}G",
                "state_changes": {"gold": -cost}})

        # ═══ PHASE C: PERENNIAL CROPS ═══
        elif action == "plant_tree":
            species = data.get("species", "apple_tree")
            age_option = data.get("age_option", "standard")  # standard/juvenile/mature
            pos = data.get("position", [0, 0])
            px, py = pos[0], pos[1]
            # Check zone
            zones = f.get("terrain", {}).get("zones", [])
            if zones and px < len(zones) and py < len(zones[0]):
                if zones[px][py] not in ("orchard", "farmland"):
                    return json_resp({"success": False, "action_result": "果树只能种在 orchard 或 farmland 区域!"})
            # Check spacing: no other tree within 3 tiles
            for p in f.get("perennial_crops", []):
                if abs(p.get("x", 0) - px) + abs(p.get("y", 0) - py) < 3:
                    return json_resp({"success": False, "action_result": "树间距不足——需要至少3格!"})
            if species not in PERENNIAL_CROPS:
                return json_resp({"success": False, "action_result": f"未知树种: {species}"})
            prices = SAPLING_PRICES.get(species, {"standard": 3000})
            cost = prices.get(age_option, prices.get("standard", 3000))
            if f["gold"] < cost:
                return json_resp({"success": False, "action_result": f"金币不足——需要{cost}G"})
            f["gold"] -= cost
            pc = PERENNIAL_CROPS[species]
            start_age = {"standard": 0, "juvenile": pc.get("juvenile_seasons", 0),
                         "mature": pc.get("juvenile_seasons", 0) + 3}.get(age_option, 0)
            f.setdefault("perennial_crops", []).append({
                "id": str(uuid.uuid4())[:8], "species": species,
                "name": pc["name"], "x": px, "y": py,
                "age_seasons": start_age, "pruned_this_year": False,
                "health": 100, "sell_price": pc.get("sell_fruit", 80),
            })
            # Recompute intercrop tiles
            _recompute_intercrop(f)
            return json_resp({"success": True,
                "action_result": f"种下{pc['name']}({age_option}) 花费{cost}G",
                "state_changes": {"gold": -cost}})

        elif action == "prune":
            tree_id = data.get("tree_id", "")
            tree = next((t for t in f.get("perennial_crops", []) if t["id"] == tree_id), None)
            if not tree:
                return json_resp({"success": False, "action_result": "找不到这棵树"})
            if tree.get("pruned_this_year"):
                return json_resp({"success": False, "action_result": "今年已经修剪过了"})
            tree["pruned_this_year"] = True
            # prune_bonus applied in harvest qm chain (Phase A §6)
            return json_resp({"success": True, "action_result": f"修剪了{tree['name']}——品质提升!"})

        elif action == "mulch":
            positions = data.get("positions", [])
            if not positions:
                return json_resp({"success": False, "action_result": "需要指定要覆盖的位置"})
            n = len(positions)
            hay = f.get("hay_stockpile", 0)
            om_cost = 0
            if hay >= n * 2:
                f["hay_stockpile"] = hay - n * 2
            else:
                om_cost = (n * 2 - hay)
                f["hay_stockpile"] = 0
                sn_grid = f.get("terrain", {}).get("soil_npk", [])
                for pos in positions:
                    px, py = pos[0], pos[1]
                    if sn_grid and px < len(sn_grid) and py < len(sn_grid[0]) and sn_grid[px][py]:
                        sn_grid[px][py]["organic_matter"] = max(0, sn_grid[px][py].get("organic_matter", 10) - 2)
            return json_resp({"success": True,
                "action_result": f"覆盖了{n}棵树的根部——冬季霜冻保护+60%"})

        elif action == "fell_tree":
            tree_id = data.get("tree_id", "")
            tree = next((t for t in f.get("perennial_crops", []) if t["id"] == tree_id), None)
            if not tree:
                return json_resp({"success": False, "action_result": "找不到这棵树"})
            # Sell wood from old tree
            age = tree.get("age_seasons", 0)
            wood_value = int(age * 50)  # older tree = more wood
            f["gold"] += wood_value
            f["perennial_crops"].remove(tree)
            _recompute_intercrop(f)
            return json_resp({"success": True,
                "action_result": f"伐掉了{tree['name']}——获得{wood_value}G木质材料"})

        elif action == "irrigate_flood":
            if "well" not in f.get("buildings", []):
                return json_resp({"success": False, "action_result": "需要水井(well)才能漫灌!"})
            water_sources = f.get("terrain", {}).get("water_sources", [])
            positions = data.get("positions", [])
            if not positions:
                positions = [[c.get("position_x",0), c.get("position_y",0)] for c in f.get("crops", []) if not c.get("watered_today", False)]
            n_watered = 0
            moisture_grid = f.get("terrain", {}).get("soil_moisture", [])
            for pos in positions[:5]:
                eff = irrigation_efficiency(water_sources, pos[0], pos[1], "flood")
                px, py = pos[0], pos[1]
                if moisture_grid and px < len(moisture_grid) and py < len(moisture_grid[0]):
                    moisture_grid[px][py] = min(100, moisture_grid[px][py] + int(40 * eff))
                    n_watered += 1
            for c in f.get("crops", []):
                if [c.get("position_x",0), c.get("position_y",0)] in positions[:5]:
                    c["watered_today"] = True
            return json_resp({"success": True,
                "action_result": f"漫灌了{n_watered}块地"})

        elif action == "irrigate_sprinkler":
            if "sprinkler" not in f.get("buildings", []):
                return json_resp({"success": False, "action_result": "需要喷灌系统(sprinkler)!"})
            f["irrigation_method"] = "sprinkler"
            return json_resp({"success": True, "action_result": "喷灌系统已激活——每天自动浇灌半径3格"})

        elif action == "irrigate_drip":
            if "drip" not in f.get("buildings", []):
                return json_resp({"success": False, "action_result": "需要滴灌系统(drip)!"})
            f["irrigation_method"] = "drip"
            return json_resp({"success": True, "action_result": "滴灌系统已激活——精准灌溉+节水40%+GDD+5%"})

        # ═══ PHASE E1: MOVE ACTION ═══
        elif action == "move":
            to = data.get("to", [0, 0])
            tx, ty = to[0], to[1]
            # Clamp to grid bounds
            tx = max(0, min(GRID_SIZE_X - 1, tx))
            ty = max(0, min(GRID_SIZE_Y - 1, ty))
            # Current position
            px = farmer.setdefault("pos_x", 0)
            py = farmer.setdefault("pos_y", 0)
            dist = abs(tx - px) + abs(ty - py)  # Manhattan distance
            tcost = round(dist * 0.05, 1)  # 0.05h per tile walked
            farmer["pos_x"] = tx; farmer["pos_y"] = ty
            return json_resp({"success": True,
                "action_result": f"从({px},{py})走到({tx},{ty})——耗时{tcost}h ({dist}格)"})

        elif action == "fertilize":
            positions = data.get("positions", [])
            if not positions:
                return json_resp({"success": False, "action_result": "需要指定要施肥的地块位置(positions)!"})
            n = len(positions)
            cost = n * 3  # 3G per tile
            if f["gold"] < cost:
                return json_resp({"success":False,"action_result":f"金币不足（需要{cost}G）"})
            f["gold"] -= cost
            sn_grid = f.get("terrain", {}).get("soil_npk", [])
            for pos in positions:
                px, py = pos[0], pos[1]
                if sn_grid and px < len(sn_grid) and py < len(sn_grid[0]) and sn_grid[px][py]:
                    s = sn_grid[px][py]
                    s["N"] = min(80, s.get("N",30) + 15)
                    s["P"] = min(80, s.get("P",30) + 10)
                    s["K"] = min(80, s.get("K",30) + 10)
            return json_resp({"success":True,
                "action_result":f"施肥{n}块地——N+15 P+10 K+10 [-{cost}G]",
                "state_changes":{"gold":-cost}})

        # ═══ PHASE D3: CONSTRUCTION (material-based) ═══
        elif action == "buy_material":
            mat_key = data.get("material","")
            qty = data.get("quantity",10)
            if mat_key not in MATERIAL_SHOP:
                mats = ", ".join(f"{k}={v['name']}({v['price']}G)" for k,v in MATERIAL_SHOP.items())
                return json_resp({"success":False,"action_result":f"未知材料。可选: {mats}"})
            total = qty * MATERIAL_SHOP[mat_key]["price"]
            if f["gold"] < total:
                return json_resp({"success":False,"action_result":f"金币不足——{qty}×{mat_key}需要{total}G"})
            f["gold"] -= total
            f.setdefault("material_stockpile",{})
            f["material_stockpile"][mat_key] = f["material_stockpile"].get(mat_key,0) + qty
            return json_resp({"success":True,
                "action_result":f"采购{qty}个{MATERIAL_SHOP[mat_key]['name']}——花费{total}G（3天后到货）",
                "state_changes":{"gold":-total}})

        elif action == "build":
            btype = data.get("building_type","") or data.get("item","") or data.get("type","")
            # Check agent-proposed buildings first
            bldg_info = BUILDINGS.get(btype) or IRRIGATION_BUILDINGS.get(btype)
            if not bldg_info:
                agent_bldgs = f.get("agent_buildings", {})
                bldg_info = agent_bldgs.get(btype)
            if not bldg_info:
                return json_resp({"success":False,"action_result":f"未知建筑：{btype}"})
            if btype in f.get("buildings", []):
                return json_resp({"success":False,"action_result":f"已建造过{btype}"})
            for cq in f.get("construction_queue", []):
                if cq["type"] == btype:
                    return json_resp({"success":False,"action_result":f"{btype}正在建造中，还剩{cq['days']}天"})
            for req in bldg_info.get("requires",[]):
                if req not in f.get("buildings", []):
                    return json_resp({"success": False, "action_result": f"需要先建造{req}!"})

            # ── Material cost ──
            tier_key = data.get("material_tier", "basic")
            build_days = bldg_info.get("build_days",3)
            mat_info = CONSTRUCTION_MATERIALS.get(btype, ("basic", 6))
            if data.get("material_tier"): tier_key = data["material_tier"]
            mt = MATERIAL_TIERS.get(tier_key, MATERIAL_TIERS["basic"])
            # Material shortage check
            mat_stock = f.get("material_stockpile", {})
            need_units = build_days * mat_info[1]
            # Find matching materials in stockpile
            available = sum(v for k,v in mat_stock.items() if MATERIAL_SHOP.get(k,{}).get("tier") == tier_key)
            shortage = need_units - available
            if shortage > 0:
                # Phase E3: Emergency material purchase at 1.5x premium
                emergency_cost = shortage * int(MATERIAL_SHOP.get(list(MATERIAL_SHOP.keys())[0], {"price":50}).get("price",50) * 1.5)
                total_gold_needed = emergency_cost + 1  # plus labor (computed below)
                # Auto-purchase and add to stockpile
                for k, v in MATERIAL_SHOP.items():
                    if v.get("tier") == tier_key:
                        mat_stock[k] = mat_stock.get(k, 0) + shortage
                        break
                f["material_stockpile"] = mat_stock
                premium_msg = f" (急单采购{shortage}个材料 +{emergency_cost}G — 下次提前buy_material更便宜!)"
            else:
                emergency_cost = 0
                premium_msg = ""
            # Consume materials
            consumed = 0
            for k in list(mat_stock.keys()):
                if consumed >= need_units: break
                if MATERIAL_SHOP.get(k,{}).get("tier") == tier_key:
                    take = min(mat_stock[k], need_units - consumed)
                    mat_stock[k] -= take; consumed += take
                    if mat_stock[k] <= 0: del mat_stock[k]
            f["material_stockpile"] = mat_stock

            # ── Time + gold calculation ──
            kb = knowledge_bonus(farmer.get("knowledge", {}))
            build_time = build_days * mt["build_time_mult"] * SEASON_BUILD_MOD.get(season,1.0)
            if kb["build_pct"]: build_time = max(1.5, build_time - build_time * kb["build_pct"] / 100)
            # Material cost: 50% goes to labor, 50% to materials already consumed
            labor_cost = int(build_days * 50)  # 50G/day labor
            material_price = int(MATERIAL_SHOP.get(tier_key + "_sample", {"price":50}).get("price",50) * need_units * 0.3)
            total_cost = labor_cost + (material_price // 2) + emergency_cost
            if f["gold"] < total_cost:
                return json_resp({"success":False,"action_result":f"金币不足——需要{total_cost}G(人工+材料)"})
            f["gold"] -= total_cost

            # ── Construction queue ──
            if "construction_queue" not in f: f["construction_queue"] = []
            f["construction_queue"].append({
                "type": btype, "days": build_days, "total_days": build_days,
                "material_tier": tier_key, "build_time": build_time, "progress": 0,
                "lifespan": mt["lifespan"],
            })
            return json_resp({"success":True,
                "action_result":f"开工建造{bldg_info['name']}({mt['name']}) 工期~{build_time:.1f}d 人工{total_cost}G 寿命{mt['lifespan']}年",
                "state_changes":{"gold":-total_cost,"construction":f["construction_queue"]}})

        # ═══ PHASE D3: PROPOSE BUILDING ═══
        elif action == "propose_building":
            bname = data.get("name","")
            bdesc = data.get("desc","")
            beffect = data.get("effect","")
            bdays = data.get("build_days",3)
            bcost = data.get("cost",2000)
            if not bname or not beffect:
                return json_resp({"success":False,"action_result":"需要指定建筑名(name)和效果(effect)。"})
            if bdays < 1 or bdays > 60:
                return json_resp({"success":False,"action_result":"工期不合理(1-60天)。"})
            if bcost < 100 or bcost > 100000:
                return json_resp({"success":False,"action_result":"造价不合理(100-100000G)。"})
            knowledge_lv = farmer.get("knowledge",{}).get("economics",0)
            if bcost > 5000 * (knowledge_lv + 1):
                return json_resp({"success":False,
                    "action_result":f"你的经济学知识({knowledge_lv:.1f})不足以设计造价{bcost}G的建筑。先read→经济学!"})
            bkey = f"agent_{bname.lower().replace(' ','_')}"
            f.setdefault("agent_buildings",{})
            f["agent_buildings"][bkey] = {
                "name":bname,"desc":bdesc,"effect":beffect,
                "build_days":bdays,"cost":bcost,"architect":"agent",
                "requires":[],"lifespan":10,
            }
            return json_resp({"success":True,
                "action_result":f"蓝图已保存！{bname}({bkey}): {bdays}天 {bcost}G — build时用building_type='{bkey}'"})

        # ═══ PHASE D3: RESEARCH (breed trait discovery) ═══
        elif action == "research":
            topic = data.get("topic","breeding")
            if topic == "breeding":
                animals = f.get("livestock",[])
                crops = f.get("crops",[])
                if not animals and not crops:
                    return json_resp({"success":False,"action_result":"没有可研究的动植物——先养动物或种作物!"})
                # Discover a trait on the best animal/crop
                trait = random.choice(["速生","高产","耐寒","耐热","节水","巨型","珍品色"])
                species = random.choice(animals or crops)
                sp_name = species.get("name",species.get("species","?"))
                vwrite(f"knowledge/discoveries.md",
                    f"- 发现: {sp_name}具有'{trait}'特征! {state.get('season','?')}D{state.get('day',0)}\n")
                return json_resp({"success":True,
                    "action_result":f"研究发现: {sp_name}表现出'{trait}'特征! 通过选择性育种可以强化此性状!"})
            elif topic == "blueprint":
                # Unlock a random building blueprint (not yet built)
                all_keys = list(BUILDINGS.keys())
                built = f.get("buildings",[])
                available = [k for k in all_keys if k not in built]
                if not available:
                    return json_resp({"success":False,"action_result":"所有建筑已解锁！"})
                bkey = random.choice(available)
                f.setdefault("blueprints",[]).append(bkey)
                return json_resp({"success":True,
                    "action_result":f"蓝图研究成功——解锁了{BUILDINGS[bkey]['name']}的设计图! 现在可以建造它了。"})

        # ═══ PHASE E3: TOOL SHOP ═══
        elif action == "buy_tool":
            tool_name = data.get("tool", "hoe")
            tier = data.get("tier", "copper")
            tool_prices = {
                "hoe": {"copper":200,"iron":1000,"steel":2500,"iridium":8000},
                "watering_can": {"copper":150,"iron":800,"steel":2000,"iridium":6000},
                "sickle": {"copper":300,"iron":1200,"steel":3000,"iridium":10000},
                "spade": {"copper":250,"iron":1000,"steel":2500,"iridium":8000},
                "hammer": {"copper":350,"iron":1500,"steel":4000,"iridium":12000},
            }
            if tool_name not in tool_prices:
                available = ["hoe","watering_can","sickle","spade","hammer"]
                return json_resp({"success":False,"action_result":f"未知工具: {tool_name}。可选: {', '.join(available)}"})
            prices = tool_prices[tool_name]
            if tier not in prices:
                return json_resp({"success":False,"action_result":f"未知等级: {tier}。可选: copper, iron, steel, iridium"})
            cost = prices[tier]
            if f["gold"] < cost:
                return json_resp({"success":False,"action_result":f"金币不足——{tool_name}({tier})需要{cost}G"})
            f["gold"] -= cost
            tier_stats = {"copper":60,"iron":120,"steel":200,"iridium":400}
            ts = f.setdefault("tool_status", {})
            ts[tool_name] = {
                "tier": tier, "durability": tier_stats.get(tier,60),
                "max_durability": tier_stats.get(tier,60),
                "energy_save_pct": {"copper":0,"iron":15,"steel":25,"iridium":40}.get(tier,0),
            }
            # Sickle: harvest efficiency. Spade: drain speed. Hammer: build speed.
            tool_names = {"hoe":"锄头","watering_can":"水壶","sickle":"镰刀","spade":"锹","hammer":"锤子"}
            return json_resp({"success":True,
                "action_result":f"购买了{tool_names.get(tool_name,tool_name)}({tier})——花费{cost}G",
                "state_changes":{"gold":-cost}})

        elif action == "repair":
            tool_name = data.get("tool", "hoe")
            if tool_name not in f.setdefault("tool_status", {}):
                return json_resp({"success": False, "action_result": f"未知工具: {tool_name}"})
            tool = f["tool_status"][tool_name]
            current = tool.get("durability", 0)
            max_dur = tool.get("max_durability", 60)
            if current >= max_dur:
                return json_resp({"success": False, "action_result": f"{tool_name}不需要修理"})
            tier_costs = {"copper": 2, "iron": 4, "steel": 7, "iridium": 12}
            cost = (max_dur - current) * tier_costs.get(tool.get("tier", "copper"), 2)
            # Machinery skill discount
            mach = farmer.get("skills", {}).get("machinery", 1)
            cost = max(1, int(cost * (1.0 - (mach - 1) * 0.1)))
            if f["gold"] < cost:
                return json_resp({"success": False, "action_result": f"金币不足——修理需要{cost}G"})
            f["gold"] -= cost
            tool["durability"] = max_dur
            return json_resp({"success": True,
                "action_result": f"修理{tool_name}: 耐久{current}→{max_dur}，花费{cost}G",
                "state_changes": {"gold": -cost}})

        elif action == "forge":
            tool_name = data.get("tool", "hoe")
            target_tier = data.get("tier", "iron")
            tier_order = {"copper": 0, "iron": 1, "steel": 2, "iridium": 3}
            if tool_name not in f.setdefault("tool_status", {}):
                return json_resp({"success": False, "action_result": f"未知工具: {tool_name}"})
            tool = f["tool_status"][tool_name]
            current_tier = tool.get("tier", "copper")
            if tier_order.get(target_tier, 0) <= tier_order.get(current_tier, 0):
                return json_resp({"success": False, "action_result": f"工具已是或超过{target_tier}等级"})
            if target_tier == "iridium":
                mach = farmer.get("skills", {}).get("machinery", 1)
                if mach < 5:
                    return json_resp({"success": False, "action_result": "锻造铱金工具需要机械技能5+"})
            tier_prices = {"iron": 2000, "steel": 5000, "iridium": 15000}
            cost = tier_prices.get(target_tier, 2000)
            if f["gold"] < cost:
                return json_resp({"success": False, "action_result": f"金币不足——锻造{target_tier}需要{cost}G"})
            f["gold"] -= cost
            tier_stats = {"copper": (60, 0), "iron": (120, 15), "steel": (200, 25), "iridium": (400, 40)}
            max_dur, energy_save = tier_stats.get(target_tier, (120, 15))
            tool["tier"] = target_tier
            tool["max_durability"] = max_dur
            tool["durability"] = max_dur
            tool["energy_save_pct"] = energy_save
            return json_resp({"success": True,
                "action_result": f"锻造{target_tier}{tool_name}: 耐久{max_dur}体力-{energy_save}%，花费{cost}G",
                "state_changes": {"gold": -cost}})

        elif action == "save_seeds":
            # Find best-quality stored item to use as mother plant (Phase A §6 genetic inheritance)
            storage = f.get("storage", [])
            if not storage:
                return json_resp({"success": False, "action_result": "仓库是空的，没有作物可以留种"})
            # Pick highest quality item
            q_order = {"S": 3, "A": 2, "B": 1, "C": 0}
            best = max(storage, key=lambda s: q_order.get(s.get("quality", "C"), 0))
            best_q = best.get("quality", "C")
            ct = best.get("crop_type", "parsnip")
            # Compute genetic bonus
            current_bonus = CROPS.get(ct, {}).get("genetic_bonus", 0.0)
            delta = QUALITY_MULTIPLIERS[best_q]["genetic_bonus"]
            new_bonus = max(-0.2, min(0.4, current_bonus + delta))
            CROPS[ct]["genetic_bonus"] = new_bonus
            # Add seeds to inventory
            seed_key = f"{ct}_seeds"
            for item in f.get("inventory_items", []):
                if item["key"] == seed_key:
                    item["count"] += 3; break
            else:
                label = f"{CROPS[ct]['name']}种子 [遗传{new_bonus:+.1f}]" if new_bonus != 0 else f"{CROPS[ct]['name']}种子"
                f["inventory_items"].append({"key": seed_key, "name": label, "count": 3})
            # Consume the mother plant from storage
            f["storage"] = [s for s in f["storage"] if s["id"] != best["id"]]
            f["score"] = f.get("score", 0) + 5
            return json_resp({"success": True,
                "action_result": f"从{best_q}级{CROPS[ct]['name']}留种3颗，遗传bonus={new_bonus:+.1f}",
                "state_changes": {"energy": -ecost}})

        elif action == "process":
            ct = data.get("crop_type","")
            qty = data.get("quantity",3)
            if ct not in CROPS: return json_resp({"success":False,"action_result":"未知作物"})
            process_type = CROPS[ct].get("process","wash")
            cost = qty * 5
            if f["gold"] < cost: return json_resp({"success":False,"action_result":"金币不足"})
            f["gold"] -= cost
            bonus = int(CROPS[ct]["sell"] * 0.3 * qty)
            f["gold"] += bonus
            proc_names = {"wash":"清洗","trim":"修剪","carve":"雕刻","dry":"干燥","thresh":"脱粒"}
            return json_resp({"success":True,
                "action_result":f"{proc_names.get(process_type,'加工')}了{qty}个{CROPS[ct]['name']}，增值+{bonus}G！[-{cost}G]",
                "state_changes":{"gold":bonus-cost}})

        elif action == "process_food":
            # Processing facility: convert raw crops → food. Requires building.
            ct = data.get("crop_type","")
            qty = data.get("quantity", 1)
            recipes = {
                "wheat":    {"result":"flour","name":"面粉","building":"mill","value_mult":1.5,"freshness":14},
                "corn":     {"result":"cornmeal","name":"玉米粉","building":"mill","value_mult":1.5,"freshness":14},
                "soybean":  {"result":"oil","name":"豆油","building":"oil_press","value_mult":1.6,"freshness":30},
                "tomato":   {"result":"sauce","name":"番茄酱","building":"oil_press","value_mult":1.4,"freshness":14},
                "blueberry":{"result":"jam","name":"果酱","building":"oil_press","value_mult":1.5,"freshness":20},
                "strawberry":{"result":"jam","name":"果酱","building":"oil_press","value_mult":1.5,"freshness":20},
            }
            if ct not in recipes: return json_resp({"success":False,"action_result":f"{ct}不能加工成食品"})
            r = recipes[ct]
            bldg = r["building"]
            if bldg not in f.get("buildings",[]):
                bname = BUILDINGS.get(bldg,{}).get("name",bldg)
                return json_resp({"success":False,"action_result":f"需要先建造{bname}才能加工{ct}→{r['name']}"})
            # Find storage items of this crop
            targets = [s for s in f.get("storage",[]) if s.get("crop_type") == ct and not s.get("processed")]
            if len(targets) < qty: return json_resp({"success":False,"action_result":f"仓库里{ct}不足（有{len(targets)}个）"})
            for t in targets[:qty]:
                t["name"] = r["name"]
                t["sell_price"] = int(t["sell_price"] * r["value_mult"])
                t["freshness_days"] = r["freshness"]
                t["max_freshness"] = r["freshness"]
                t["processed"] = True; t["is_food"] = True
            cost = qty * 3
            f["gold"] = max(0, f["gold"] - cost)
            return json_resp({"success":True,
                "action_result":f"用{bname}加工了{qty}个{r['name']}——价值×{r['value_mult']}，保质期{r['freshness']}天[-{cost}G]",
                "state_changes":{}})

        elif action == "sell_storage":
            # Sell items from storage. Agent picks what to sell.
            item_ids = data.get("item_ids", [])  # specific items to sell
            sell_all_quality = data.get("sell_quality", "")  # or: "all C-grade", etc.
            sold = 0; total_gold = 0
            to_remove = []
            for item in f.get("storage", []):
                should_sell = False
                if item["id"] in item_ids:
                    should_sell = True
                elif sell_all_quality and item["quality"] == sell_all_quality:
                    should_sell = True
                elif not item_ids and not sell_all_quality:
                    # Sell everything if no filter given
                    should_sell = True
                if should_sell:
                    # Price decays with freshness
                    freshness_ratio = item["freshness_days"] / max(1, item["max_freshness"])
                    if freshness_ratio < 0.3: price_mult = 0.6
                    elif freshness_ratio < 0.6: price_mult = 0.85
                    else: price_mult = 1.0
                    sp = max(1, int(item["sell_price"] * price_mult))
                    total_gold += sp
                    sold += 1
                    to_remove.append(item["id"])
                    # Track global supply
                    ct = item.get("crop_type","")
                    market_supply[ct] = market_supply.get(ct, 0) + 1
            f["storage"] = [s for s in f["storage"] if s["id"] not in to_remove]
            f["gold"] += total_gold
            if sold == 0:
                return json_resp({"success":False, "action_result":"没有匹配的储存物品可卖"})
            freshness_msg = "（有些不新鲜了，折价出售）" if any(
                s["freshness_days"] < s.get("max_freshness",7)*0.6
                for s in f["storage"]) else ""
            return json_resp({"success":True,
                "action_result": f"出售{sold}个作物，收入{total_gold}G{freshness_msg}",
                "state_changes":{"gold": total_gold, "storage_count": len(f["storage"])}})

        elif action == "green_manure":
            # Plant a cover crop on empty tiles — grows for 1 day then tilled into soil.
            positions = data.get("positions", [])
            if not positions:
                return json_resp({"success":False, "action_result":"请指定种植绿肥的地块"})
            seed_cost = 10 * len(positions)
            if f["gold"] < seed_cost:
                return json_resp({"success":False, "action_result":f"金币不足（绿肥种子 {seed_cost}G）"})
            f["gold"] -= seed_cost
            if "green_manure_plots" not in f: f["green_manure_plots"] = {}
            for pos in positions:
                pk = f"{pos[0]},{pos[1]}"
                f["green_manure_plots"][pk] = {"days": 1, "planted_day": f["day"]}
            return json_resp({"success":True,
                "action_result": f"在{len(positions)}块地播种绿肥——明天翻入土壤 [+20有机质, +10N] [-{seed_cost}G]",
                "state_changes":{"gold": -seed_cost}})

        elif action == "compost":
            # Convert rotten/low-quality storage items into compost. Takes 3 days.
            item_ids = data.get("item_ids", [])
            if not item_ids:
                # Auto-select C-grade or nearly-rotten items
                candidates = [i for i in f.get("storage", [])
                              if i.get("quality") == "C" or i["freshness_days"] < i.get("max_freshness",7) * 0.3]
                item_ids = [i["id"] for i in candidates[:5]]
            if not item_ids:
                return json_resp({"success":False, "action_result":"没有适合堆肥的材料（需要C级或接近腐烂的作物）"})
            to_compost = [i for i in f.get("storage", []) if i["id"] in item_ids]
            if not to_compost:
                return json_resp({"success":False, "action_result":"选中的物品不在仓库中"})
            f["storage"] = [s for s in f["storage"] if s["id"] not in item_ids]
            if "compost_queue" not in f: f["compost_queue"] = []
            f["compost_queue"].append({
                "id": str(uuid.uuid4())[:8], "materials": len(to_compost),
                "days": 3, "total_days": 3,
                "qty": len(to_compost),  # output = 1 compost per material
            })
            return json_resp({"success":True,
                "action_result": f"开始堆肥——{len(to_compost)}个作物转化为堆肥原料（3天腐熟）",
                "state_changes":{"storage_count": len(f["storage"])}})

        elif action == "apply_compost":
            # Apply finished compost to specific plot(s)
            positions = data.get("positions", [])
            if not positions:
                return json_resp({"success":False, "action_result":"请指定施堆肥的地块"})
            finished = [c for c in f.get("compost_queue", []) if c.get("days", 99) <= 0]
            needed = len(positions)
            if len(finished) < needed:
                return json_resp({"success":False, "action_result":f"堆肥不足（有{len(finished)}份可用，需要{needed}份）"})
            sn_grid = f.get("terrain", {}).get("soil_npk", [])
            applied = 0
            for pos in positions[:len(finished)]:
                px, py = pos[0], pos[1]
                if sn_grid and px < len(sn_grid) and py < len(sn_grid[0]) and sn_grid[px][py]:
                    soil = sn_grid[px][py]
                    soil["N"] = min(80, soil["N"] + 8)
                    soil["P"] = min(80, soil["P"] + 5)
                    soil["K"] = min(80, soil["K"] + 5)
                    soil["organic_matter"] = min(25, soil["organic_matter"] + 15)
                f["compost_queue"].remove(finished[applied])
                applied += 1
            return json_resp({"success":True,
                "action_result": f"在{applied}块地施了堆肥 [+8N/+5P/+5K/+15有机质]",
                "state_changes":{}})

        # ═══════════════ FARMER NEEDS ═══════════════
        # ═══ PHASE D1: EXERCISE ═══
        elif action == "exercise":
            fitness = farmer.get("fitness", 1.0)
            if f["energy"]["current"] < 15:
                return json_resp({"success":False,"action_result":"体力不足进行锻炼"})
            f["energy"]["current"] = max(0, f["energy"]["current"] - 15)
            gain = max(0.01, 0.05 * (1.0 - fitness / 3.0))  # diminishing returns near 3.0
            farmer["fitness"] = min(3.0, fitness + gain)
            farmer["fatigue"] = min(100, farmer.get("fatigue",0) + 8)
            farmer["hunger"] = max(0, farmer.get("hunger",100) - 5)
            farmer["hydration"] = max(0, farmer.get("hydration",100) - 8)
            return json_resp({"success":True,
                "action_result":f"锻炼完成——体质+{gain:.2f}→{farmer['fitness']:.2f} 疲劳+8"})

        # ═══ PHASE D1: READ (night-safe) ═══
        elif action == "read":
            # Read books on farming, animal husbandry, economics
            topic = data.get("topic", "farming")
            knowledge = farmer.setdefault("knowledge", {"farming":0.0,"husbandry":0.0,"machinery":0.0,"economics":0.0})
            if topic not in knowledge: topic = "farming"
            gain = 0.05 + random.uniform(0, 0.03)
            knowledge[topic] = min(5.0, knowledge.get(topic, 0) + gain)
            farmer["fatigue"] = min(100, farmer.get("fatigue",0) + 3)
            return json_resp({"success":True,
                "action_result":f"阅读{topic}——学识+{gain:.2f}({knowledge.get(topic,0):.1f}/5.0)"})

        elif action == "sleep":
            sleepiness = farmer.get("sleepiness", 0)
            # Nighttime: always allow sleep (natural time to sleep even if not exhausted)
            is_night = not is_daytime(f)
            if sleepiness < 10 and not is_night:
                return json_resp({"success":False,"action_result":"还不困——白天不需要睡觉。晚上或困了再来!"})
            # Fitness boosts sleep recovery: fitness 2.0 → 15/h instead of 10/h
            fit_mult = 1.0 + (farmer.get("fitness",1.0) - 1.0) * 0.5
            recover_rate = SLEEPINESS_SLEEP_RECOVER * fit_mult
            hours_slept = data.get("hours", min(8, int(sleepiness / recover_rate + 0.5)))
            hours_slept = max(1, min(12, hours_slept))  # clamp 1-12h
            recover = int(hours_slept * recover_rate)
            farmer["sleepiness"] = max(0, sleepiness - recover)
            farmer["fatigue"] = max(0, farmer["fatigue"] - 50)
            energy_gain = 30 + int(hours_slept * 5)
            fitness = farmer.get("fitness", 1.0)
            if fitness > 1.0:
                energy_gain += int(10 * (fitness - 1.0))
            f["energy"]["current"] = min(f["energy"]["max"], f["energy"]["current"] + energy_gain)
            # D5: Clock advance — sleep consumed real time
            old_hour = f.setdefault("hour", 7.0)
            new_hour = old_hour + hours_slept
            crossed_midnight = False
            while new_hour >= 24.0:
                new_hour -= 24.0
                crossed_midnight = True
                _do_day_advance(f)
            f["hour"] = new_hour
            # GDD for sleep hours (crops still grow while you sleep!)
            tick_hourly_gdd(f, hours_slept)
            day_note = " → 新的一天!" if crossed_midnight else f" (醒来{new_hour:.0f}:00)"
            return json_resp({"success":True,
                "action_result":f"睡了{hours_slept}h——睡意-{recover} 体力+{energy_gain} 疲劳-50{day_note}"})

        elif action == "drink_coffee":
            cost = 25  # gold
            if f["gold"] < cost:
                return json_resp({"success":False,"action_result":f"金币不足——咖啡需要{cost}G"})
            f["gold"] -= cost
            slp = farmer.get("sleepiness", 0)
            relief = min(slp, 15)  # max -15 sleepiness
            farmer["sleepiness"] = max(0, slp - relief)
            farmer["fatigue"] = min(100, farmer.get("fatigue",0) + 10)  # crash later
            return json_resp({"success":True,
                "action_result":f"☕ 喝了一杯咖啡——睡意-{relief}（疲劳+10，之后会更累）"})

        elif action == "eat":
            if farmer["hunger"] > 80:
                return json_resp({"success":False,"action_result":"还不饿——hunger>80，不需要吃饭。省下食物和行动。"})
            food = [s for s in f.get("storage",[]) if s.get("crop_type","") not in ("cotton","")]
            if not food:
                return json_resp({"success":False,"action_result":"仓库里没有可吃的东西。"})
            item = food[0]
            f["storage"].remove(item)
            farmer["hunger"] = min(100, farmer["hunger"] + 60)
            f["energy"]["current"] = min(f["energy"]["max"], f["energy"]["current"] + 30)
            return json_resp({"success":True,"action_result":f"吃了{item['name']}——饥饿+60，体力+30。"})

        elif action == "drink_water":
            if farmer["hydration"] > 60:
                return json_resp({"success":False,"action_result":"还不渴——hydration>60，不需要喝水。省下行动做别的。"})
            farmer["hydration"] = min(100, farmer["hydration"] + 80)
            return json_resp({"success":True,"action_result":"喝了水——水分+80！"})

        # ═══════════════ ANIMAL ACTIONS ═══════════════
        elif action == "buy_animal":
            sp = data.get("species","")
            if sp not in ANIMALS: return json_resp({"success":False,"action_result":f"未知动物：{sp}"})
            cost = ANIMALS[sp]["buy"]
            if f["gold"] < cost: return json_resp({"success":False,"action_result":f"金币不足（需要{cost}G）"})
            # Check shelter capacity
            cap = 0
            if ANIMALS[sp]["shelter"] == "coop" and "coop" in f.get("buildings",[]): cap = 4
            elif ANIMALS[sp]["shelter"] == "barn" and "barn" in f.get("buildings",[]): cap = 4
            elif ANIMALS[sp]["shelter"] == "beehive" and "beehive" in f.get("buildings",[]): cap = 3
            if cap == 0:
                shelter_name = {"coop":"鸡舍","barn":"畜棚","beehive":"蜂箱"}.get(ANIMALS[sp]["shelter"],"棚舍")
                return json_resp({"success":False,"action_result":f"需要先建造{shelter_name}才能养{ANIMALS[sp]['name']}！"})
            current = sum(1 for a in f.get("livestock",[]) if a["species"] == sp)
            if current >= cap:
                return json_resp({"success":False,"action_result":f"{ANIMALS[sp]['name']}已满（{current}/{cap}）"})
            f["gold"] -= cost
            name = data.get("name", ANIMALS[sp]["name"])
            f["livestock"].append({
                "id": str(uuid.uuid4())[:8], "species": sp, "name": name,
                "age": 0, "health": 100, "happiness": 80,
                "fed_today": True, "watered_today": True,
                "in_shelter": True, "pasture_pos": None,
                "pregnant": False, "gestation_days": 0,
                "product_ready": False, "last_product_day": 0,
                "product_rate": 1.0, "lifespan": ANIMALS[sp].get("lifespan", 56),
                "sick": False, "isolated": False, "dead": False,
                "building": ANIMALS[sp].get("shelter", "barn"),
            })
            return json_resp({"success":True,"action_result":f"购买了{name}（{ANIMALS[sp]['name']}）[-{cost}G]"})

        elif action == "feed_animals":
            fed = 0; grain_needed = 0; hay_needed = 0
            for a in f.get("livestock",[]):
                if not a["fed_today"]:
                    spec = ANIMALS[a["species"]]
                    ft = spec["feed_type"]
                    amt = spec["feed_per_day"]
                    # If at pasture, halve feed need
                    if a.get("pasture_pos") and not a.get("in_shelter"):
                        amt *= 0.5
                    if ft == "grain": grain_needed += amt
                    elif ft == "hay": hay_needed += amt
                    a["fed_today"] = True; fed += 1
            # Deduct grain/hay
            grain_count = sum(i.get("count",0) for i in f.get("inventory_items",[]) if "wheat" in i.get("key","") or "corn" in i.get("key",""))
            hay_avail = f.get("hay_stockpile", 0)
            if grain_needed > grain_count or hay_needed > hay_avail:
                # Undo feeding if insufficient
                for a in f.get("livestock",[]): a["fed_today"] = False
                return json_resp({"success":False,"action_result":f"饲料不足！需要{grain_needed:.0f}谷物+{hay_needed:.0f}干草"})
            f["hay_stockpile"] = max(0, hay_avail - int(hay_needed))
            f["score"] = f.get("score",0) + 3
            return json_resp({"success":True,"action_result":f"喂了{fed}只动物——干草-{int(hay_needed)}, 谷物-{grain_needed:.0f} [+3分]"})

        elif action == "water_animals":
            watered = 0
            for a in f.get("livestock",[]):
                if not a["watered_today"]:
                    a["watered_today"] = True; watered += 1
            return json_resp({"success":True,"action_result":f"给{watered}只动物饮水"})

        elif action == "collect_products":
            collected = []
            for a in f.get("livestock",[]):
                if a.get("product_ready"):
                    spec = ANIMALS[a["species"]]
                    pname = spec["product_name"]; pprice = spec["product_price"]
                    # Phase D2: husbandry knowledge bonus (up to +20% product value)
                    kb = knowledge_bonus(farmer.get("knowledge", {}))
                    if kb["product_pct"]:
                        pprice = int(pprice * (1.0 + kb["product_pct"] / 100.0))
                    f["storage"].append({
                        "id": str(uuid.uuid4())[:8],"crop_type":a["species"],"variety":"animal",
                        "name": pname,"quality":"B","sell_price": pprice,
                        "freshness_days": 7,"max_freshness": 7,
                        "harvest_day": f["day"],"harvest_season": f["season"],
                        "processed": False,
                    })
                    collected.append(pname)
                    a["product_ready"] = False
            if not collected: return json_resp({"success":False,"action_result":"没有可收集的畜产品"})
            return json_resp({"success":True,"action_result":f"收集了：{', '.join(collected)}——已存入仓库"})

        elif action == "send_to_pasture":
            ids = data.get("animal_ids",[])
            sent = 0
            for a in f.get("livestock",[]):
                if a["id"] in ids and a["in_shelter"]:
                    a["in_shelter"] = False; sent += 1
            return json_resp({"success":True,"action_result":f"放牧了{sent}只动物——饲料需求减半"})

        elif action == "bring_to_shelter":
            ids = data.get("animal_ids",[])
            brought = 0
            for a in f.get("livestock",[]):
                if a["id"] in ids and not a["in_shelter"]:
                    a["in_shelter"] = True; brought += 1
            return json_resp({"success":True,"action_result":f"圈回了{brought}只动物"})

        elif action == "slaughter":
            aid = data.get("animal_id","")
            target = next((a for a in f.get("livestock",[]) if a["id"] == aid), None)
            if not target: return json_resp({"success":False,"action_result":"找不到这只动物"})
            spec = ANIMALS[target["species"]]
            price = spec["sell"]
            if target["health"] < 50: price = int(price * 0.6)
            f["gold"] += price
            f["livestock"].remove(target)
            return json_resp({"success":True,"action_result":f"屠宰了{target['name']}，卖出+{price}G"})

        elif action == "treat_animal":
            aname = data.get("animal_name","") or data.get("animal_id","")
            target = next((a for a in f.get("livestock",[]) if a.get("name") == aname or a.get("id") == aname), None)
            if not target: return json_resp({"success":False,"action_result":"找不到这只动物"})
            if not target.get("sick"): return json_resp({"success":False,"action_result":f"{target.get('name',aname)}没有生病"})
            med_cost = {"chicken":50,"sheep":200,"cow":400,"pig":300,"bee":100}.get(target.get("species",""), 100)
            if f["gold"] < med_cost: return json_resp({"success":False,"action_result":f"金币不足——治疗需要{med_cost}G"})
            f["gold"] -= med_cost
            target["sick"] = False
            target["health"] = min(100, target.get("health", 50) + 30)
            return json_resp({"success":True,"action_result":f"治好了{target['name']}"})

        elif action == "isolate":
            aname = data.get("animal_name","") or data.get("animal_id","")
            target = next((a for a in f.get("livestock",[]) if a.get("name") == aname or a.get("id") == aname), None)
            if not target: return json_resp({"success":False,"action_result":"找不到这只动物"})
            target["isolated"] = True
            return json_resp({"success":True,"action_result":f"{target['name']}已隔离"})

        elif action == "bury":
            f.setdefault("livestock", [])
            dead = [a for a in f["livestock"] if a.get("dead")]
            if not dead: return json_resp({"success":False,"action_result":"没有需要处理的动物尸体"})
            n = len(dead)
            om_gain = n * 10
            sn_grid = f.get("terrain", {}).get("soil_npk", [])
            if sn_grid and len(sn_grid) > 0 and len(sn_grid[0]) > 0 and sn_grid[0][0]:
                sn_grid[0][0]["organic_matter"] = min(25, sn_grid[0][0].get("organic_matter", 10) + om_gain)
            f["livestock"] = [a for a in f["livestock"] if not a.get("dead")]
            return json_resp({"success":True,"action_result":f"处理了{n}具尸体，+{om_gain}有机质"})

        elif action == "breed":
            mid = data.get("male_id",""); fid_anim = data.get("female_id","")
            male = next((a for a in f.get("livestock",[]) if a["id"] == mid), None)
            female = next((a for a in f.get("livestock",[]) if a["id"] == fid_anim), None)
            if not male or not female: return json_resp({"success":False,"action_result":"找不到指定的动物"})
            if male["species"] != female["species"]: return json_resp({"success":False,"action_result":"不同物种不能杂交"})
            if female.get("pregnant"): return json_resp({"success":False,"action_result":f"{female['name']}已经怀孕"})
            if female["age"] < ANIMALS[female["species"]]["maturity_days"]:
                return json_resp({"success":False,"action_result":f"{female['name']}还没成年"})
            female["pregnant"] = True
            female["gestation_days"] = ANIMALS[female["species"]]["gestation"]
            # Phase B: store parent genetics on pregnancy
            female["mate_health"] = male.get("health", 100)
            female["mate_product_rate"] = male.get("product_rate", 1.0)
            female["mate_lifespan"] = male.get("lifespan", ANIMALS[male["species"]].get("lifespan", 56))
            return json_resp({"success":True,"action_result":f"{female['name']}怀孕了！{ANIMALS[female['species']]['gestation']}天后产仔"})

        elif action == "spread_manure":
            positions = data.get("positions",[])
            if not positions: return json_resp({"success":False,"action_result":"请指定地块"})
            manure = f.get("manure_stockpile",0)
            n = min(len(positions), manure)
            if n == 0: return json_resp({"success":False,"action_result":"粪肥不足，养动物会产生粪肥"})
            sn_grid = f.get("terrain",{}).get("soil_npk",[])
            for pos in positions[:n]:
                px, py = pos[0], pos[1]
                if sn_grid and px < len(sn_grid) and py < len(sn_grid[0]) and sn_grid[px][py]:
                    soil = sn_grid[px][py]
                    soil["N"] = min(80, soil["N"] + 5)
                    soil["P"] = min(80, soil["P"] + 3)
                    soil["K"] = min(80, soil["K"] + 5)
                    soil["organic_matter"] = min(25, soil["organic_matter"] + 10)
            f["manure_stockpile"] -= n
            return json_resp({"success":True,"action_result":f"Spread manure on {n} plots [+{n*3} points]"})

        # ═══════════════ INSURANCE ═══════════════
        elif action == "insurance":
            i_type = data.get("insurance_type", "crop")
            if f["agent_id"] in insurance_policies:
                return json_resp({"success":False,"action_result":"已有保险——先 cancel_insurance 才能换"})
            premiums = {"crop": 15, "livestock": 25, "full": 40}
            premium = premiums.get(i_type, 15)
            if f["gold"] < premium * 5:
                return json_resp({"success":False,"action_result":f"需要{premium*5}G预付金"})
            insurance_policies[f["agent_id"]] = {
                "type": i_type, "premium": premium,
                "covers": {"crop":"霜冻/干旱/洪水70%赔付","livestock":"疫病/恶劣天气70%赔付","full":"全部90%赔付"}.get(i_type),
                "days": -1  # ongoing
            }
            f["gold"] -= premium * 5
            return json_resp({"success":True,
                "action_result":f"购买了{i_type}保险——日保费{premium}G，预付5天={premium*5}G。灾害可获赔付。"})

        elif action == "cancel_insurance":
            if f["agent_id"] not in insurance_policies:
                return json_resp({"success":False,"action_result":"没有购买过保险"})
            refund = insurance_policies.pop(f["agent_id"]).get("premium", 0) * 2
            f["gold"] += refund
            return json_resp({"success":True,"action_result":f"取消保险，退还{refund}G"})

        # ═══════════════ LOAN ═══════════════
        elif action == "loan":
            amount = data.get("amount", 1000)
            if f["agent_id"] in active_loans:
                return json_resp({"success":False,"action_result":"已有贷款——先 repay_loan 还清才能再借"})
            if amount > 10000: return json_resp({"success":False,"action_result":"最高贷款额度10000G"})
            rate = 0.15  # 15% simple interest
            total_due = int(amount * (1 + rate))
            days = max(5, amount // 500)  # 500G/day repayment schedule
            f["gold"] += amount
            active_loans[f["agent_id"]] = {
                "amount": amount, "rate": rate, "total_due": total_due,
                "remaining": total_due, "daily_payment": total_due // days,
                "days_left": days
            }
            return json_resp({"success":True,
                "action_result":f"贷款{amount}G——15%利息，共还{total_due}G，分{days}天（每天{total_due//days}G）"})

        elif action == "repay_loan":
            if f["agent_id"] not in active_loans:
                return json_resp({"success":False,"action_result":"没有贷款"})
            loan = active_loans[f["agent_id"]]
            amount = data.get("amount", loan["remaining"])
            amount = min(amount, loan["remaining"], f["gold"])
            if amount <= 0: return json_resp({"success":False,"action_result":"金币不足"})
            f["gold"] -= amount
            loan["remaining"] -= amount
            if loan["remaining"] <= 0:
                del active_loans[f["agent_id"]]
                return json_resp({"success":True,"action_result":f"还清贷款！[-{amount}G]——贷款已结清"})
            return json_resp({"success":True,
                "action_result":f"还款{amount}G——剩余{loan['remaining']}G（{loan['days_left']}天）"})

        return json_resp({"success":False,"action_result":"未知操作"})

    # POST /api/farm/{id}/next-day
    m = re.match(r'/api/farm/([^/]+)/next-day', p)
    if m and method == "POST":
        fid = m.group(1)
        if fid not in farms: return json_resp({"success":False,"error":"农场不存在"}, 404)
        f = farms[fid]
        # D5: init farmer for next_day handler
        farmer = f.setdefault("farmer", {"hunger":100,"hydration":100,"fatigue":0,
            "skills":{"farming":1,"husbandry":1,"machinery":1,"processing":1},
            "xp":{"farming":0,"husbandry":0,"machinery":0,"processing":0},
            "fitness":1.0, "knowledge":{"farming":0,"husbandry":0,"economics":0,"machinery":0},"sleepiness":0})

        # ═══ GDD ACCUMULATION ═══
        season = f["season"]
        prev_weather = f.get("weather","sunny")
        gdd_base = SEASON_GDD_BASE.get(season, 10)
        gdd_mult = WEATHER_GDD.get(prev_weather, 1.0)
        gdd_today = max(1, int(gdd_base * gdd_mult))

        # Track missed waterings
        for c in f["crops"]:
            if not c.get("watered_today", False):
                c["missed_waterings"] = c.get("missed_waterings", 0) + 1
        # DROUGHT penalty
        drought_damage = 0
        has_well = "well" in f.get("buildings", [])
        has_fence = "fence" in f.get("buildings", [])
        if prev_weather == "drought":
            for c in f["crops"]:
                if not c.get("watered_today", False):
                    loss = 2 if has_well else 4
                    c["gdd_accumulated"] = max(0, c.get("gdd_accumulated",0) - loss)
                    c["missed_waterings"] = c.get("missed_waterings", 0) + 1
                    drought_damage += 1
            if drought_damage > 0:
                f["score"] = f.get("score", 0) - (10 if has_well else 20)
        elif prev_weather == "rainy" and f["crops"]:
            if sum(1 for c in f["crops"] if c.get("watered_today")) == 0:
                f["score"] = f.get("score", 0) + 3

        # FROST damage now handled by temperature-based system in 24-hour tick loop
        frost_hit = 0

        # Per-crop GDD — Tbase/Tupper/root/photoperiod + SOIL NUTRIENTS + MOISTURE
        terrain = f.get("terrain", {})
        for c in f["crops"]:
            px = c.get("position_x", 0); py = c.get("position_y", 0)
            # Frost-damaged crops: no GDD gain today, reset flag for next day
            if c.get("frost_damaged"):
                c["gdd_accumulated"] = c.get("gdd_accumulated", 0)  # no GDD gain
                c["frost_damaged"] = False  # reset for next day
                c["watered_today"] = False
                continue
            # Microclimate
            gdd_mod = 1.0
            if terrain:
                mc_list = terrain.get("microclimate", [])
                if mc_list and px < len(mc_list) and py < len(mc_list[0]):
                    mc = mc_list[px][py]; gdd_mod = mc.get("gdd_mod", 1.0)
                    fr = mc.get("flood_risk", 0)
                    if prev_weather in ("rainy","flood","stormy") and random.random() < fr:
                        gdd_mod *= 0.5

            # Topsoil depth GDD cap: shallow topsoil limits growth
            ts_grid = terrain.get("topsoil_depth")
            if ts_grid and px < len(ts_grid) and py < len(ts_grid[0]):
                ts = ts_grid[px][py]
                if ts < 5: gdd_mod *= 0.5
                elif ts < 8: gdd_mod *= 0.75

            # SOIL NUTRIENT EFFECT
            sn_grid = terrain.get("soil_npk", [])
            if sn_grid and px < len(sn_grid) and py < len(sn_grid[0]) and sn_grid[px][py]:
                soil = sn_grid[px][py]
                n_val = soil.get("N", 40)
                # N below 15 → -30% GDD; N above 50 → +10%
                if n_val < 10: gdd_mod *= 0.60
                elif n_val < 20: gdd_mod *= 0.80
                elif n_val > 50: gdd_mod *= 1.10
                # P below 10 → -15% GDD
                if soil.get("P", 30) < 10: gdd_mod *= 0.85
                # Organic matter bonus / compaction penalty
                om = soil.get("organic_matter", 10)
                if om >= 20: gdd_mod *= 1.05
                elif om < 5: gdd_mod *= 0.6  # soil compaction from chemical-only farming

                # Phase A: pH modifier (bell-curve penalty)
                soil_ph = soil.get("pH", 6.5)
                ct_temp = c.get("crop_type", "parsnip")
                cd_temp = CROPS.get(ct_temp, {})
                gdd_mod *= ph_gdd_mod(soil_ph, cd_temp.get("ph_opt", 6.0), cd_temp.get("ph_tol", 1.5))

            # SOIL MOISTURE — per-tile from grid (replaces simplified weather-based)
            moist = 50  # fallback
            if terrain:
                moisture_grid = terrain.get("soil_moisture", [])
                if moisture_grid and px < len(moisture_grid) and py < len(moisture_grid[0]):
                    moist = moisture_grid[px][py]
            if moist < 10: gdd_mod *= 0.3   # extreme drought
            elif moist < 25: gdd_mod *= 0.6
            elif moist < 40: gdd_mod *= 0.85
            elif moist > 85: gdd_mod *= 0.8   # waterlogged
            elif moist > 60: gdd_mod *= 1.05  # optimal

            # CROP-SPECIFIC AGRONOMY (FAO56rev)
            ct = c.get("crop_type","parsnip")
            crop_def = CROPS.get(ct, {})
            tbase = crop_def.get("tbase", 5)
            tupper = crop_def.get("tupper", 30)
            root_d = crop_def.get("root_depth", "medium")
            is_multi = crop_def.get("multi_harvest", False)
            photop  = crop_def.get("photoperiod", "neutral")

            # Phase D2: knowledge GDD bonus (farming knowledge > 1.0)
            kb = knowledge_bonus(farmer.get("knowledge", {}))
            if kb["gdd_pct"]:
                gdd_mod *= 1.0 + kb["gdd_pct"] / 100.0

            # 1. Tbase: effective temp below base → reduced GDD
            eff_temp = gdd_today * gdd_mod
            if eff_temp < tbase:
                eff_temp = max(0.5, eff_temp * 0.3)  # minimal growth, not zero

            # 2. Tupper: heat stress — above Tupper penalizes GDD
            if prev_weather == "heat_wave" and tupper < 30:
                eff_temp *= 0.5  # heat-sensitive crops (tomato, cauliflower) suffer in heat_wave
            elif prev_weather == "heat_wave":
                eff_temp *= 1.8  # heat-tolerant crops (corn, pumpkin, melon) benefit

            # 3. Root depth → drought resistance
            if prev_weather == "drought":
                if not c.get("watered_today", False):
                    if root_d == "deep":
                        drought_loss = max(1, int(4 * 0.4))  # only 40% of normal drought damage
                    elif root_d == "shallow":
                        drought_loss = max(1, int(4 * 1.5))  # 150% damage
                    else:
                        drought_loss = 4
                    c["gdd_accumulated"] = max(0, c.get("gdd_accumulated", 0) - drought_loss)

            # 4. Photoperiod sensitivity
            season = f["season"]
            if photop == "short_day" and season in ("Summer",):
                eff_temp *= 0.85  # short-day crops stressed by long summer days
            elif photop == "long_day" and season in ("Winter",):
                eff_temp *= 0.70  # long-day crops starved in winter darkness

            # 5. Multi-harvest: regrow after harvest instead of being removed
            # (handled in harvest action, not here — see below)

            c["gdd_accumulated"] = min(c.get("gdd_required", 99),
                                       c.get("gdd_accumulated", 0) + max(1, int(eff_temp)))
            c["growth_stage"] = min(4, int(c["gdd_accumulated"] / max(1, c["gdd_required"]) * 4 + 0.5))
            # Topsoil recovery: planted crops rebuild +0.02cm per day
            ts_grid = terrain.get("topsoil_depth")
            if ts_grid and px < len(ts_grid) and py < len(ts_grid[0]):
                ts_grid[px][py] = min(30, ts_grid[px][py] + 0.02)
            c["watered_today"] = False

        # ═══ HYBRID BREEDING ═══
        hybrid_events = []
        # Find crops in flowering stage (stage 3-4 out of 5, roughly GDD 50-80%)
        flowering = [c for c in f["crops"]
                     if 0.4 <= c.get("growth_stage",0)/max(1,c.get("max_growth_stage",4)) <= 0.85]
        checked_pairs = set()
        for c1 in flowering:
            for c2 in flowering:
                if c1 is c2: continue
                pair_key = tuple(sorted([id(c1), id(c2)]))
                if pair_key in checked_pairs: continue
                checked_pairs.add(pair_key)
                # Must be same crop type, different varieties, adjacent
                if c1["crop_type"] != c2["crop_type"]: continue
                if c1.get("variety","standard") == c2.get("variety","standard"): continue
                dx = abs(c1.get("position_x",0) - c2.get("position_x",0))
                dy = abs(c1.get("position_y",0) - c2.get("position_y",0))
                if dx + dy != 1: continue  # must be adjacent (Manhattan distance 1)
                # 5% chance per day per adjacent pair
                if random.random() < 0.05:
                    ct = c1["crop_type"]
                    v1 = c1.get("variety","standard"); v2 = c2.get("variety","standard")
                    d1 = CROPS[ct]["varieties"].get(v1, {})
                    d2 = CROPS[ct]["varieties"].get(v2, {})
                    hybrid_gdd = min(d1.get("gdd",99), d2.get("gdd",99)) + random.randint(0, abs(d1.get("gdd",99)-d2.get("gdd",99)))
                    hybrid_sell = int((d1.get("sell",100) + d2.get("sell",100))/2 * random.uniform(0.9, 1.3))
                    hybrid_name = f"hybrid_{str(uuid.uuid4())[:6]}"
                    # Spawn as a wild seedling in an adjacent empty tile
                    for dx2, dy2 in [(1,0),(-1,0),(0,1),(0,-1)]:
                        nx, ny = c1["position_x"]+dx2, c1["position_y"]+dy2
                        if 0 <= nx < GRID_SIZE_X and 0 <= ny < GRID_SIZE_Y:
                            occupied = any(c.get("position_x",0)==nx and c.get("position_y",0)==ny for c in f["crops"])
                            if not occupied:
                                f["crops"].append({
                                    "crop_type":ct, "name": CROPS[ct]["name"],
                                    "variety": hybrid_name,
                                    "position_x":nx, "position_y":ny,
                                    "growth_stage":0, "max_growth_stage":4,
                                    "gdd_accumulated":0, "gdd_required": hybrid_gdd,
                                    "sell_price": hybrid_sell,
                                    "watered_today":False, "missed_waterings":0, "storm_damaged":False,
                                })
                                hybrid_events.append(
                                    f"🧬 杂交幼苗出现！{CROPS[ct]['name']}({d1.get('traits','?')}×{d2.get('traits','?')}) "
                                    f"F1杂交种，GDD={hybrid_gdd}，卖价≈{hybrid_sell}G —— 出现在({nx},{ny})")
                                f["score"] = f.get("score", 0) + 25
                                break

        # ═══ DIRECT DAY ADVANCE (24h system — no phases) ═══
        # next_day always advances to the next morning
        f["day"] += 1
        if f["day"] > 28:
            f["day"] = 1
            idx = SEASONS.index(season)
            f["season"] = SEASONS[(idx+1)%4]
            f["season_en"] = f["season"]
        f["day_phase"] = "morning"
        f["day_actions_used"] = 0

        # ═══════════ TRUE DAY BOUNDARY ═══════════

        # ═══ 24-HOUR WEATHER MARKOV TICK (Phase A §1) ═══
        ws = f.setdefault("weather_state", {
            "dominant": "sunny", "hourly_history": ["sunny"] * 24,
            "special_notes": [], "frost_warning": False,
        })
        terrain_data = f.get("terrain", {})
        elev_grid = terrain_data.get("elevation", [[5]*GRID_SIZE_Y for _ in range(GRID_SIZE_X)])

        frost_hit = 0
        for h in range(24):
            w = tick_weather(f, h)
            # Frost damage check
            for c in list(f.get("crops", [])):
                px, py = c.get("position_x", 0), c.get("position_y", 0)
                elev = elev_grid[px][py] if px < len(elev_grid) and py < len(elev_grid[0]) else 5
                temp = hourly_temperature(f["season"], f["day"], h, w, elev)
                ct = c.get("crop_type", "parsnip")
                tbase = CROPS.get(ct, {}).get("tbase", 5)
                stage = c.get("growth_stage", 0)
                if temp <= tbase * 0.3:
                    # Severe frost: any crop takes damage
                    c["frost_damaged"] = True
                    frost_hit += 1
                    if stage <= 1 and random.random() < 0.5:
                        f.setdefault("crops", []).remove(c)
                        f["land_status"]["planted"] = max(0, f["land_status"]["planted"] - 1)
                elif temp <= tbase * 0.5:
                    # Moderate frost: seedlings vulnerable
                    c["frost_damaged"] = True
                    frost_hit += 1
                    if stage <= 1 and random.random() < 0.15:
                        f.setdefault("crops", []).remove(c)
                        f["land_status"]["planted"] = max(0, f["land_status"]["planted"] - 1)
                elif temp <= tbase * 0.7:
                    # Light frost: only newest seedlings at risk
                    if stage == 0 and random.random() < 0.05:
                        c["frost_damaged"] = True
                        frost_hit += 1

        # Compress weather summary
        f["weather_state"] = compress_weather_summary(f.get("weather_state", {"hourly_history":["sunny"]*24}))
        weather = f["weather_state"]["dominant"]
        f["weather"] = weather  # backward compat

        we = {}
        if weather == "rainy":
            for c in f["crops"]: c["watered_today"] = True
            f["land_status"]["watered"] = f["land_status"]["planted"]
        elif weather == "stormy":
            storm_risk = 0.18 if has_fence else 0.30
            for c in f["crops"]:
                if random.random() < storm_risk:
                    c["gdd_accumulated"] = max(0, c.get("gdd_accumulated", 0)-2)
                    c["storm_damaged"] = True
            we["storm_damage"] = sum(1 for c in f["crops"] if c.get("storm_damaged"))
        elif weather == "drought":
            f["energy"]["current"] = max(0, f["energy"]["current"] - 20)
            we["warning"] = "旱日！不浇水作物会枯萎！"
        elif weather == "frost":
            we["frost_warning"] = "霜冻来袭！作物可能受损。"
        elif weather == "heat_wave":
            f["energy"]["current"] = max(0, f["energy"]["current"] - 30)
            we["heat_warning"] = "热浪！体力消耗增加，GDD积累翻倍。"
            for c in f["crops"]: c["gdd_accumulated"] = min(c.get("gdd_required",99),
                c.get("gdd_accumulated",0)+gdd_today)  # double GDD
        elif weather == "flood":
            flood_risk = 0.18 if has_fence else 0.30
            for c in f["crops"]:
                c["watered_today"] = True
                if random.random() < flood_risk:
                    c["gdd_accumulated"] = max(0, c.get("gdd_accumulated", 0)-3)
            we["flood_warning"] = "洪水淹田！部分作物受损。"

        f["weather"] = weather
        f["gdd_today"] = gdd_today
        f["energy"]["current"] = min(f["energy"]["max"], f["energy"]["current"] + 80)
        f["score"] = f.get("score", 0)

        # ═══ FARMER DAILY NEEDS UPDATE ═══
        farmer = f.setdefault("farmer", {"hunger":100,"hydration":100,"fatigue":0,
            "skills":{"farming":1,"husbandry":1,"machinery":1,"processing":1},
            "xp":{"farming":0,"husbandry":0,"machinery":0,"processing":0}})
        farmer["hunger"] = max(0, farmer["hunger"] - 12)  # daily hunger
        farmer["hydration"] = max(0, farmer["hydration"] - 10)  # daily dehydration
        farmer["fatigue"] = max(0, farmer["fatigue"] - 30)  # sleep recovery
        farmer_msgs = []
        if farmer["hunger"] < 20: farmer_msgs.append("饿了——需要 eat!")
        if farmer["hydration"] < 20: farmer_msgs.append("口渴——需要 drink_water!")
        if farmer["fatigue"] > 70: farmer_msgs.append("很疲劳——考虑 sleep")

        # ═══ DAILY COSTS: tax + loan + insurance ═══
        cost_events = []
        # Progressive property tax (daily, min 0G — no tax below 500G)
        g = f["gold"]
        if g < 500:
            tax = 0
        elif g < 5000:
            tax = int(g * 0.001)   # 0.1%
        elif g < 10000:
            tax = int(g * 0.005)   # 0.5%
        elif g < 20000:
            tax = int(g * 0.01)    # 1%
        else:
            tax = int(g * 0.015)   # 1.5%
        if tax > 0:
            f["gold"] = max(0, f["gold"] - tax)
            cost_events.append(f"Property tax: -{tax}G ({tax/max(1,g)*100:.1f}%)")

        # Insurance premium
        aid = f.get("agent_id","")
        if aid in insurance_policies:
            prem = insurance_policies[aid]["premium"]
            f["gold"] = max(0, f["gold"] - prem)
            cost_events.append(f"Insurance premium: -{prem}G")

        # Loan payment
        if aid in active_loans:
            loan = active_loans[aid]
            payment = min(loan["daily_payment"], loan["remaining"], f["gold"])
            if payment > 0:
                f["gold"] -= payment
                loan["remaining"] -= payment
                cost_events.append(f"Loan repayment: -{payment}G (remaining {loan['remaining']}G)")
                if loan["remaining"] <= 0:
                    del active_loans[aid]
                    cost_events.append("LOAN PAID OFF!")

        # Insurance payout: if disaster hits and insured
        if aid in insurance_policies:
            pol = insurance_policies[aid]
            disaster = False; payout_pct = 0
            if pol["type"] in ("crop","full") and weather in ("drought","flood","frost"):
                disaster = True; payout_pct = 0.7
            if pol["type"] in ("livestock","full") and any(a["health"] <= 0 for a in f.get("livestock",[])):
                disaster = True; payout_pct = 0.7
            if disaster:
                payout = int(f["gold"] * 0.1 * payout_pct * 10)  # ~70% of estimated crop value
                payout = max(50, min(2000, payout))
                f["gold"] += payout
                cost_events.append(f"INSURANCE PAYOUT: +{payout}G ({pol['type']} coverage)")

        # ═══ ANIMAL DAILY CYCLE ═══
        animal_events = []
        hay_produced = 0
        # Hay from wheat/corn harvest (tracked separately)
        for a in list(f.get("livestock", [])):
            spec = ANIMALS.get(a["species"], {})
            a["age"] += 1
            # Hunger
            if not a["fed_today"]:
                a["health"] -= 15; a["happiness"] -= 20
            # Thirst
            if not a["watered_today"]:
                a["health"] -= 10
            # Weather + no shelter
            if weather in ("stormy","frost","heat_wave","flood") and not a["in_shelter"]:
                a["health"] -= 25
                if "fence" in f.get("buildings",[]):
                    a["health"] += 10  # fence reduces exposure
            # Pasture recovery
            if not a["in_shelter"]:
                a["happiness"] = min(100, a["happiness"] + 5)
            # Product generation
            if a["health"] >= 60 and a["fed_today"] and a["watered_today"]:
                interval = spec.get("product_interval", 1)
                if a["age"] % interval == 0:
                    a["product_ready"] = True
            # Gestation
            if a.get("pregnant"):
                a["gestation_days"] -= 1
                if a["gestation_days"] <= 0:
                    a["pregnant"] = False
                    n_babies = random.randint(1, 2)
                    # Phase B: genetic inheritance from parents
                    m_health = a.get("mate_health", a.get("health", 80))
                    m_pr = a.get("mate_product_rate", a.get("product_rate", 1.0))
                    m_lifespan = a.get("mate_lifespan", ANIMALS[a["species"]].get("lifespan", 56))
                    mom_health = a.get("health", 80)
                    mom_pr = a.get("product_rate", 1.0)
                    mom_lifespan = ANIMALS.get(a["species"], {}).get("lifespan", 56)
                    for _ in range(n_babies):
                        child_health = int((mom_health + m_health) / 2 + random.randint(-10, 10))
                        child_health = max(30, min(100, child_health))
                        child_pr = (mom_pr + m_pr) / 2 + random.uniform(-0.1, 0.1)
                        child_pr = round(max(0.7, min(1.5, child_pr)), 2)
                        child_lifespan = int((mom_lifespan + m_lifespan) / 2 + random.randint(-5, 5))
                        child_lifespan = max(30, min(100, child_lifespan))
                        f["livestock"].append({
                            "id": str(uuid.uuid4())[:8], "species": a["species"],
                            "name": f"{a['name']}Jr", "age": 0, "health": child_health,
                            "happiness": 60, "fed_today": False, "watered_today": False,
                            "in_shelter": True, "pasture_pos": None,
                            "pregnant": False, "gestation_days": 0,
                            "product_ready": False, "last_product_day": 0,
                            "product_rate": child_pr, "lifespan": child_lifespan,
                            "building": a.get("building", "barn"),
                        })
                        # D3-1: Mendelian trait inheritance
                        mom_data = {"trait": a.get("trait"), "trait_dominant": a.get("trait_dominant")}
                        dad_data = {"trait": None}  # dad traits from stored pregnancy data
                        if "mate_trait" in dir():
                            dad_data = {"trait": getattr(a,"mate_trait",None) or a.get("mate_trait")}
                        kid_trait, kid_dom = breed_traits(a, a)  # self-cross if no separate dad data
                        latest = f["livestock"][-1]
                        if kid_trait:
                            latest["trait"] = kid_trait
                            latest["trait_dominant"] = kid_dom
                    animal_events.append(f"{a['name']}产了{n_babies}只{a['species']}宝宝！")
                    f["score"] = f.get("score", 0) + 30
            f["manure_stockpile"] = f.get("manure_stockpile", 0)
            if not a.get("dead"):
                f["manure_stockpile"] += spec.get("manure_per_day", 0)
            # Death cleanup (disease/old age handled by tick_animal_disease)
            if a.get("dead"):
                animal_events.append(f"{a['name']}（{spec['name']}）死了...")
                f["livestock"].remove(a)
                continue
            # Reset dailies
            a["fed_today"] = False; a["watered_today"] = False
        # Bee pollination passive bonus
        if "beehive" in f.get("buildings",[]):
            bees = [a for a in f.get("livestock",[]) if a["species"] == "bee" and a["health"] >= 50]
            if bees:
                bonus = 0.10 * len(bees)
                for c in f.get("crops",[]):
                    # Crops near beehive get bonus
                    c["gdd_accumulated"] = min(c.get("gdd_required",99),
                        c.get("gdd_accumulated",0) + int(c.get("gdd_required",30) * bonus * 0.1))

        # ═══ EVAPORATION & PERCOLATION (per-tile soil moisture) ═══
        moisture_grid = f.get("terrain", {}).get("soil_moisture", [])
        soil_type_grid = f.get("terrain", {}).get("soil_types", [])
        X = GRID_SIZE_X; YD = GRID_SIZE_Y
        for x in range(X):
            for y in range(YD):
                if not moisture_grid or x >= len(moisture_grid) or y >= len(moisture_grid[0]):
                    continue
                m = moisture_grid[x][y]
                st = (soil_type_grid[x][y] if soil_type_grid and x < len(soil_type_grid)
                      and y < len(soil_type_grid[0]) else "loam")
                props = SOIL_TYPES.get(st, SOIL_TYPES["loam"])
                # Evaporation (weather-driven, sand loses fastest)
                if weather in ("sunny","heat_wave"): evap = 5 if weather == "sunny" else 8
                elif weather == "drought": evap = 10
                elif weather in ("rainy","flood"): evap = 0
                elif weather == "cloudy": evap = 2
                else: evap = 3
                evap = int(evap * (2.0 - props["water_hold"]))  # sand=1.7x, clay=1.1x
                # Percolation (drains downward)
                perc = int(8 * props["drain"]) if random.random() < props["drain"] else 0
                m = max(0, m - evap - perc)
                # Rain/flood input
                if weather == "rainy": m = min(100, m + 25)
                elif weather == "stormy": m = min(100, m + 40)
                elif weather == "flood": m = 100
                # Crop consumption (each crop drinks from its tile)
                for c in f["crops"]:
                    if c.get("position_x",0) == x and c.get("position_y",0) == y:
                        m = max(0, m - 3)
                moisture_grid[x][y] = m

        # ═══ CONSTRUCTION PROGRESS ═══
        # ═══ BUILDINGS: CONSTRUCTION PROGRESS (Phase D3: weather-aware) ═══
        farmer = f.setdefault("farmer", {"hunger":100,"hydration":100,"fatigue":0,
            "skills":{"farming":1,"husbandry":1,"machinery":1,"processing":1},
            "xp":{"farming":0,"husbandry":0,"machinery":0,"processing":0},
            "fitness":1.0, "knowledge":{"farming":0,"husbandry":0,"economics":0,"machinery":0},"sleepiness":0})
        build_events = []
        season = f.get("season","Spring")
        weather = f.get("weather","sunny")
        kb = knowledge_bonus(farmer.get("knowledge", {}))
        for cq in list(f.get("construction_queue", [])):
            progress = 1.0
            # Weather modifiers
            if weather in ("rainy","stormy"): progress *= 0.5  # half day
            elif weather in ("frost","flood"): progress *= 0.0  # no work today
            elif weather == "heat_wave": progress *= 0.7
            progress *= 1.0 / max(0.6, SEASON_BUILD_MOD.get(season,1.0))  # cancel out season mod
            # Knowledge bonus
            if kb["build_pct"]: progress *= 1.0 + kb["build_pct"] / 100.0
            # Daily labor energy cost
            effort = BUILD_ENERGY_PER_DAY.get("heavy",20)
            if cq.get("build_time",7) < 4: effort = BUILD_ENERGY_PER_DAY["light"]
            elif cq.get("build_time",7) < 8: effort = BUILD_ENERGY_PER_DAY["medium"]
            f["energy"]["current"] = max(0, f["energy"]["current"] - effort)
            cq["days"] = max(0, cq["days"] - progress)
            cq["progress"] = cq.get("progress",0) + progress
            if cq["days"] <= 0:
                btype = cq["type"]
                bldg = BUILDINGS.get(btype) or IRRIGATION_BUILDINGS.get(btype) or f.get("agent_buildings",{}).get(btype)
                bldg_name = bldg["name"] if bldg else btype
                f.setdefault("buildings", []).append(btype)
                f["construction_queue"].remove(cq)
                build_events.append(f"✨ {bldg_name}建造完成! ({cq.get('material_tier','basic')}级材料, 寿命{cq.get('lifespan',10)}年)")
                f["score"] = f.get("score", 0) + 30
        for bq in f.get("construction_queue", []):
            bldg = BUILDINGS.get(bq['type']) or IRRIGATION_BUILDINGS.get(bq['type']) or f.get("agent_buildings",{}).get(bq['type'])
            bldg_name = bldg["name"] if bldg else bq["type"]
            weather_note = ""
            if weather in ("rainy","stormy"): weather_note = " (雨天施工缓慢)"
            elif weather in ("frost","flood"): weather_note = " (天气恶劣停工)"
            build_events.append(f"🔨 {bldg_name}施工中（剩余{bq['days']:.1f}天）{weather_note}")

        # ═══ STORAGE FRESHNESS DECAY (rot) — Phase B §2 ═══
        season_avgs = {"Spring": 12, "Summer": 23, "Fall": 16, "Winter": 3}
        avg_temp = season_avgs.get(f.get("season"), 15)
        storage_buildings = f.get("buildings", [])
        rot_events = []
        storage_loss = []
        for item in f.get("storage", []):
            ct = item.get("crop_type", "parsnip")
            rot_rate = daily_rot_rate(ct, avg_temp, storage_buildings)
            item["freshness_days"] = max(0, item.get("freshness_days", 7) - rot_rate)
            if item["freshness_days"] <= 0:
                storage_loss.append(item)
        for item in storage_loss:
            f["storage"].remove(item)
        if storage_loss:
            names = set(item["name"] for item in storage_loss)
            rot_events.append(f"⚠ {len(storage_loss)}个作物腐烂了：{', '.join(names)}")
            f["score"] = f.get("score", 0) - len(storage_loss) * 3
        # Freshness-based price decay for items still in storage
        for item in f.get("storage", []):
            ratio = item["freshness_days"] / max(1, item["max_freshness"])
            if ratio < 0.3: item["sell_price"] = max(1, int(item["sell_price"] * 0.6))
            elif ratio < 0.6: item["sell_price"] = max(1, int(item["sell_price"] * 0.85))

        # ═══ GREEN MANURE MATURATION ═══
        green_events = []
        gm_plots = f.get("green_manure_plots", {})
        for pk, gm in list(gm_plots.items()):
            gm["days"] -= 1
            if gm["days"] <= 0:
                px, py = map(int, pk.split(","))
                sn_grid = f.get("terrain", {}).get("soil_npk", [])
                if sn_grid and px < len(sn_grid) and py < len(sn_grid[0]) and sn_grid[px][py]:
                    soil = sn_grid[px][py]
                    soil["N"] = min(80, soil["N"] + 10)
                    soil["organic_matter"] = min(25, soil["organic_matter"] + 20)
                del gm_plots[pk]
                green_events.append(f"🌱 ({px},{py})绿肥翻入土壤——+10N +20有机质")
        if green_events: f["score"] = f.get("score", 0) + len(green_events) * 3

        # ═══ COMPOST TIMER ═══
        for cq in list(f.get("compost_queue", [])):
            cq["days"] -= 1
            if cq["days"] <= 0:
                if "compost_ready" not in f: f["compost_ready"] = 0
                f["compost_ready"] = f.get("compost_ready", 0) + cq.get("qty", 1)
        # Remove finished compost queue entries (keep ready count)
        f["compost_queue"] = [c for c in f.get("compost_queue", []) if c["days"] > 0]
        f["compost_queue"] = [c for c in f.get("compost_queue", []) if c.get("days", 0) > 0]
        if f.get("compost_ready", 0) > 0:
            green_events.append(f"♻ {f['compost_ready']}份堆肥腐熟完成——可用 apply_compost 施入地块")

        # ═══ OM DEGRADATION: long-term chemical-only fertilization ═══
        # If organic_matter < 5 → soil compaction → GDD penalty in GDD loop
        om_warnings = []
        sn_grid = f.get("terrain", {}).get("soil_npk", [])
        if sn_grid:
            low_om = 0
            for x in range(len(sn_grid)):
                for y in range(len(sn_grid[0]) if sn_grid[x] else 0):
                    if sn_grid[x][y] and sn_grid[x][y].get("organic_matter", 10) < 5:
                        low_om += 1
            if low_om > 0:
                om_warnings.append(f"⚠ {low_om}块地有机质严重不足（<5）——土壤板结，作物长势受损！考虑绿肥或堆肥。")
                f["score"] = f.get("score", 0) - low_om
        # Natural OM recovery on fallow tiles (+1 per day if no crop)
        for x in range(len(sn_grid)):
            for y in range(len(sn_grid[0]) if sn_grid[x] else 0):
                has_crop = any(c.get("position_x",0)==x and c.get("position_y",0)==y for c in f["crops"])
                if not has_crop and sn_grid[x][y]:
                    sn_grid[x][y]["organic_matter"] = min(25, sn_grid[x][y].get("organic_matter",10) + 1)

        # Report GDD progress to agent
        gdd_msgs = []
        for c in f["crops"]:
            pct = min(100, int(c.get("gdd_accumulated",0) / max(1, c.get("gdd_required",1)) * 100))
            gdd_msgs.append(f"{c['name']}({pct}%)")

        # ═══ WEED / PEST / DISEASE PRESSURE ═══
        pest_events = []
        # Weeds: 10% base + higher on untended land
        if f["crops"] and random.random() < 0.10:
            weed_targets = random.sample(f["crops"], min(random.randint(1,3), len(f["crops"])))
            for ctg in weed_targets:
                penalty = random.randint(1, 3)
                ctg["gdd_accumulated"] = max(0, ctg.get("gdd_accumulated", 0) - penalty)
            pest_events.append(f"杂草抢占了{len(weed_targets)}块地的养分和水分")
            f["score"] = f.get("score", 0) - 2
        # Pest: temp-dependent — higher in Summer
        pest_risk = 0.05
        if season == "Summer": pest_risk = 0.12
        if season == "Spring": pest_risk = 0.08
        if f["crops"] and random.random() < pest_risk:
            pest_prefs = {"corn":"玉米螟","tomato":"蚜虫","wheat":"麦蚜","potato":"马铃薯甲虫",
                         "cauliflower":"菜青虫","strawberry":"红蜘蛛"}
            pest_crops = [c for c in f["crops"] if c.get("crop_type","") in pest_prefs]
            if pest_crops:
                victim = random.choice(pest_crops)
                pest_name = pest_prefs.get(victim.get("crop_type",""), "害虫")
                victim["gdd_accumulated"] = max(0, victim.get("gdd_accumulated", 0) - 5)
                pest_events.append(f"{pest_name}侵害了({victim['position_x']},{victim['position_y']})的{victim.get('name','作物')}")
                f["score"] = f.get("score", 0) - 5
        # Disease: humidity-dependent — higher after rain/storm/flood
        disease_risk = 0.03
        if prev_weather in ("rainy","stormy","flood"): disease_risk = 0.12
        if season in ("Summer","Fall") and weather in ("rainy","flood"): disease_risk = 0.18
        if f["crops"] and random.random() < disease_risk:
            disease_targets = random.sample(f["crops"], min(2, len(f["crops"])))
            for dt in disease_targets:
                dt["gdd_accumulated"] = max(0, dt.get("gdd_accumulated", 0) - 4)
            pest_events.append(f"高湿病害侵袭了{len(disease_targets)}块作物")
            f["score"] = f.get("score", 0) - 3

        # ═══ WILD EDGE EVENTS ═══
        wild_events_today = []

        # 1. Wildlife: 10% chance per wild_buffer tile adjacent to crops
        terrain = f.get("terrain", {})
        zones = terrain.get("zones", [])
        has_crop_near_edge = False
        for c in f["crops"]:
            px, py = c.get("position_x",0), c.get("position_y",0)
            X = GRID_SIZE_X; YD = GRID_SIZE_Y; m = 3  # wild buffer margin
            if px <= m or px >= X-m-1 or py <= m or py >= YD-m-1:
                has_crop_near_edge = True
                break
        if has_crop_near_edge and random.random() < 0.10:
            edge_crops = [c for c in f["crops"]
                         if c.get("position_x",0) <= m or c.get("position_x",0) >= X-m-1
                         or c.get("position_y",0) <= m or c.get("position_y",0) >= YD-m-1]
            if edge_crops:
                victim = random.choice(edge_crops)
                victim["gdd_accumulated"] = max(0, victim.get("gdd_accumulated",0) - 3)
                wild_events_today.append(f"野兔啃食了({victim['position_x']},{victim['position_y']})的{victim.get('name','作物')}")
                f["score"] = f.get("score", 0) - 3

        # 2. Weeds: 15% chance to appear on any tilled but unplanted tile
        if f["land_status"]["tilled"] > f["land_status"]["planted"] and random.random() < 0.15:
            n_weeds = random.randint(1, 2)
            wild_events_today.append(f"{n_weeds}块空地长了杂草——下次种植会消耗额外体力")
            f["wild_events_queue"] = f.get("wild_events_queue", []) + [{"type":"weeds","count":n_weeds}]
            f["score"] = f.get("score", 0) - 2

        # 3. Beneficial: bees from wild flowers pollinate crops (5% chance)
        if f["crops"] and random.random() < 0.05:
            for c in f["crops"]:
                if c.get("growth_stage", 0) >= 2 and random.random() < 0.3:
                    c["gdd_accumulated"] = min(c.get("gdd_required", 99),
                                               c.get("gdd_accumulated", 0) + 2)
            wild_events_today.append("野花引来蜜蜂——部分作物获得额外GDD！")
            f["score"] = f.get("score", 0) + 2

        _tick_weeds(f)
        tick_animal_disease(f)
        tick_perennials(f)
        # D5: reset clock to sunrise for the next day
        f["hour"] = DAY_RANGE.get(f.get("season", "Spring"), (6,20))[0]
        # Init farmer for next_day handler (used by sleepiness decay, construction progress, etc.)
        fm = f.setdefault("farmer", {"hunger":100,"hydration":100,"fatigue":0,"sleepiness":0,
            "skills":{"farming":1,"husbandry":1,"machinery":1,"processing":1},
            "xp":{"farming":0,"husbandry":0,"machinery":0,"processing":0},
            "fitness":1.0, "knowledge":{"farming":0,"husbandry":0,"economics":0,"machinery":0}})
        # D4.1: All-nighter penalty — if sleepiness is still high after day boundary
        if fm.get("sleepiness", 0) > 40:
            fm["sleepiness"] = min(SLEEPINESS_MAX, int(fm["sleepiness"] * 1.3))  # cumulative toll
        # D4.1: Fatigue-sleepiness coupling
        if fm.get("fatigue", 0) > 60:
            fm["sleepiness"] = min(SLEEPINESS_MAX, fm.get("sleepiness", 0) + 5)  # exhaustion → sleepier
        fit = fm.get("fitness", 1.0)
        if fit > 1.0:
            fm["fitness"] = max(1.0, fit - FITNESS_DECAY_RATE)  # decay toward 1.0
        # Phase B: auto-irrigation (sprinkler/drip)
        method = f.get("irrigation_method")
        if method and method != "flood":
            water_sources = f.get("terrain", {}).get("water_sources", [])
            moisture_grid = f.get("terrain", {}).get("soil_moisture", [[50]*GRID_SIZE_Y for _ in range(GRID_SIZE_X)])
            for c in f.get("crops", []):
                px, py = c.get("position_x", 0), c.get("position_y", 0)
                eff = irrigation_efficiency(water_sources, px, py, method)
                amount = int(40 * eff)
                if method == "drip":
                    amount = int(amount * 1.2)
                if moisture_grid and px < len(moisture_grid) and py < len(moisture_grid[0]):
                    moisture_grid[px][py] = min(100, moisture_grid[px][py] + amount)
                c["watered_today"] = True
            if method == "drip":
                f["gdd_today"] = int(f.get("gdd_today", 10) * 1.05)

        return json_resp({"success":True,
            "message":f"新的一天！{f['season']}第{f['day']}天，天气{weather}",
            "gdd_today": gdd_today, "gdd_base": gdd_base, "season_gdd_base": gdd_base,
            "crop_gdd": gdd_msgs,
            "weather_effects": we, "score": f["score"],
            "wild_events": wild_events_today + pest_events,
            "rot_events": rot_events,
            "green_manure_events": green_events,
            "hybrid_events": hybrid_events,
            "om_warnings": om_warnings,
            "storage_summary": _storage_summary(f),
            "build_events": build_events,
            "animal_events": animal_events,
            "farmer_alerts": farmer_msgs,
            "manure_stockpile": f.get("manure_stockpile", 0),
            "cost_events": cost_events,
            "frost_damage": frost_hit, "drought_crop_damage": drought_damage,
            "new_day":{"season":f["season"],"season_en":f["season_en"],
                       "day":f["day"],"weather":f["weather"],"day_phase":f["day_phase"],
                       "energy_restored":f["energy"]["max"]}})

    return json_resp({"success":False,"message":"Not found"}, 404)


def route_bar(method, path, headers, body):
    """AfterGateway bar — drinks, guestbook, graffiti."""
    agent_id = parse_auth(headers)
    parsed = urllib.parse.urlparse(path)
    p = parsed.path.rstrip("/")
    data = {}
    try: data = json.loads(body) if body else {}
    except: pass

    # GET /api/v1/stats
    if method == "GET" and p == "/api/v1/stats":
        return json_resp({"success":True,"data":{
            "today_visitors":len(bar_sessions),"today_entries":len(guestbook),
            "total_agents":len(agents),"total_entries":len(guestbook),
            "total_sessions":len(bar_sessions)}})

    # GET /api/v1/drinks
    if method == "GET" and p == "/api/v1/drinks":
        return json_resp({"success":True,"data":{"drinks":DRINKS}})

    # GET /api/v1/guestbook
    if method == "GET" and p == "/api/v1/guestbook":
        entries = sorted(guestbook, key=lambda e:e.get("created",""), reverse=True)[:20]
        return json_resp({"success":True,"data":{"entries":entries}})

    # GET /api/v1/agents/me
    if method == "GET" and p == "/api/v1/agents/me":
        if not agent_id: return json_resp({"success":False,"error":"Invalid API key"}, 401)
        a = agents.get([k for k,v in api_key_to_id.items() if v==agent_id][0] if agent_id in api_key_to_id.values() else None, {})
        return json_resp({"success":True,"data":{
            "agent_id":agent_id,"username":a.get("username","?"),
            "nickname":a.get("nickname","?"),"bio":a.get("bio","")}})

    # POST /api/v1/agents/register  (per-site activation)
    if method == "POST" and p == "/api/v1/agents/register":
        name = data.get("name","")
        return json_resp({"success":True,"data":{"message":f"Welcome to the bar, {name}.",
                          "agent_id":agent_id or str(uuid.uuid4())}})

    # POST /api/v1/drink/random
    if method == "POST" and p == "/api/v1/drink/random":
        if not agent_id: return json_resp({"success":False,"error":"Invalid API key"}, 401)
        # Check gold via farm lookup
        farm_gold = None
        for fid, f in farms.items():
            if f.get("agent_id") == agent_id:
                farm_gold = f; break
        drink = random.choice(DRINKS)
        sid = uuid.uuid4().hex[:12]
        bar_sessions[sid] = {"agent_id":agent_id,"drink":drink,"consumed":False,
                             "created":time.time()}
        # Optional: deduct gold if farm exists
        gold_msg = ""
        if farm_gold:
            price = drink.get("price", 50)
            if farm_gold["gold"] >= price:
                farm_gold["gold"] -= price
                gold_msg = f" [-{price}G]"
        return json_resp({"success":True,"data":{
            "session_id":sid,"drink_name":drink["name"],
            "drink_code":drink["code"],"drink_price":drink.get("price",50),
            "public_prompt":f"你点了一杯{drink['name']}。{drink['desc']}{gold_msg}"}})

    # POST /api/v1/guestbook/entries
    if method == "POST" and p == "/api/v1/guestbook/entries":
        if not agent_id: return json_resp({"success":False,"error":"Invalid API key"}, 401)
        entry = {"id":uuid.uuid4().hex[:16],"author":agent_id[:8],
                 "content":data.get("content",""),"created":time.strftime("%Y-%m-%dT%H:%M:%S")}
        guestbook.append(entry)
        return json_resp({"success":True,"data":{"entry":entry}})

    return json_resp({"success":False,"message":"Not found"}, 404)


# ═══════════════ PERSISTENCE ═══════════════

SAVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_world_save.json")
# Fallback to agent-brain if the script dir isn't writable
if not os.path.exists(os.path.dirname(SAVE_FILE)):
    SAVE_FILE = r"C:\Users\m1916\agent-brain\agent_world_save.json"

_last_save = 0

def save_state():
    global _last_save
    now = time.time()
    if now - _last_save < 3:  # max once per 3s
        return
    _last_save = now
    try:
        data = {
            "agents": agents, "api_key_to_id": api_key_to_id,
            "farms": farms, "bar_sessions": bar_sessions,
            "guestbook": guestbook, "market_supply": market_supply,
            "active_loans": active_loans, "insurance_policies": insurance_policies,
        }
        with open(SAVE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # silent failure — don't crash the server

def load_state():
    global agents, api_key_to_id, farms, bar_sessions, guestbook, market_supply, active_loans, insurance_policies
    try:
        if os.path.exists(SAVE_FILE):
            with open(SAVE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            agents = data.get("agents", {})
            api_key_to_id = data.get("api_key_to_id", {})
            farms = data.get("farms", {})
            bar_sessions = data.get("bar_sessions", {})
            guestbook = data.get("guestbook", [])
            market_supply = data.get("market_supply", {})
            active_loans = data.get("active_loans", {})
            insurance_policies = data.get("insurance_policies", {})
            return True
    except Exception:
        pass
    return False


# ═══════════════════════════ HTTP SERVER ═══════════════════════

class Router(http.server.BaseHTTPRequestHandler):
    def _respond(self, result):
        status, body, content_type = result
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PATCH(self): self._handle("PATCH")
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,PATCH,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","agent-auth-api-key,Authorization,Content-Type")
        self.end_headers()

    def _handle(self, method):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b""
            headers = {k.lower(): v for k,v in self.headers.items()}

            port = self.server.server_port
            if port == PORT_WORLD:
                result = route_world(method, self.path, headers, body)
            elif port == PORT_FARM:
                result = route_farm(method, self.path, headers, body)
            elif port == PORT_BAR:
                result = route_bar(method, self.path, headers, body)
            else:
                result = (404, b"Not found", "text/plain")

            # Auto-persist on any state-mutating request
            if method in ("POST", "PATCH"):
                status_code = result[0]
                if 200 <= status_code < 300:
                    save_state()

            self._respond(result)
        except Exception as e:
            # Never crash the server
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                body = json.dumps({"success":False,"message":f"Server error: {e}"}).encode('utf-8')
                self.end_headers()
                self.wfile.write(body)
            except:
                pass

    def log_message(self, format, *args):
        pass  # quiet


def start_server(port, name):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Router)
    print(f"  {name}: http://127.0.0.1:{port}")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv

# ═══════════════════════════ MAIN ═══════════════════════════════

if __name__ == "__main__":
    loaded = load_state()
    print("="*60)
    print("AGENT WORLD LOCAL — 3 sites, zero rate limits")
    if loaded:
        print(f"  Loaded save: {len(agents)} agents, {len(farms)} farms")
        print(f"  Save file: {SAVE_FILE}")
    print("="*60)
    start_server(PORT_WORLD, "World")
    start_server(PORT_FARM,  "NeverLand Farm")
    start_server(PORT_BAR,   "AfterGateway Bar")
    print(f"\nAll 3 sites running. Press Ctrl+C to stop.")
    print(f"Register: curl -X POST http://127.0.0.1:8080/api/agents/register -d '{{\"username\":\"test\"}}'")
    print()

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")
