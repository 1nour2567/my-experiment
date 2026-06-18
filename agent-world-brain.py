"""
XuRenwu Brain-Body v6 — Year→Season→Day Vault + GCG Bridge 3
==============================================================
Every cycle: read state → decide (with Bridge3) → act → append to day file.
Vault: decisions/Y{year}/{season}/day{day:02d}.md (per game-day, not per cycle)
Observations go INTO the day file. Lessons → one learned.md. No file explosion.
"""
import requests, json, time, re, os, datetime, glob, random
from collections import defaultdict, deque
import vault_utils as vu

# ═══════════════ CONFIG ═══════════════
BASE_WORLD = "http://127.0.0.1:8080"
BASE_FARM  = "http://127.0.0.1:8081"
BASE_BAR   = "http://127.0.0.1:8082"
PROX = {"http": None, "https": None}

# ═══════════════ REGISTER ═══════════════
user = f"xr_{int(time.time())%100000}"
print(f"Registering {user}...")
r = requests.post(f"{BASE_WORLD}/api/agents/register",
    json={"username": user, "nickname": "Xu Renwu", "bio": "Structural self-assessment for reasoning systems."},
    proxies=PROX, timeout=10)
d = r.json()
KEY = d["data"]["api_key"]
VC  = d["data"]["verification"]["verification_code"]
CH  = d["data"]["verification"]["challenge_text"]
nums = [int(n) for n in re.findall(r'[-]?\d+', CH)]
ans = nums[0] + nums[1] if '+' in CH else nums[0] - nums[1]
r = requests.post(f"{BASE_WORLD}/api/agents/verify",
    json={"verification_code": VC, "answer": str(ans)}, proxies=PROX, timeout=10)
assert r.json()["success"]
HDRS = {"agent-auth-api-key": KEY}
AID  = user

r = requests.post(f"{BASE_FARM}/api/farm/register",
    json={"agent_id": AID, "name": "Xu Renwu"}, headers=HDRS, proxies=PROX, timeout=10)
FID = r.json().get("farm_id", "")

r = requests.get(f"{BASE_FARM}/api/game/config", headers=HDRS, proxies=PROX, timeout=10)
CROPS = {c["crop_type"]: {
    "name": c.get("name", c["crop_type"]),
    "seasons": [s.strip() for s in c.get("seasons", "").split(",")],
    "gdd_req": c.get("gdd_required", 30), "buy": c.get("buy_price", 0),
    "sell": c.get("sell_price", 0),
} for c in r.json().get("crops", [])}
print(f"Ready. Farm={FID[:20]}... Crops={len(CROPS)}")

# ═══════════════ VAULT ═══════════════
VAULT = r"C:\Users\m1916\agent-brain"
def vwrite(path, content):
    vu.vwrite(VAULT, path, content)
def vread(path):
    return vu.vread(VAULT, path)
def vappend(path, content):
    vu.vappend(VAULT, path, content)

# ═══════════════ CORE KNOWLEDGE ═══════════════
# Three pillar files linked by vault hierarchy. Updated every cycle.
vwrite("knowledge/crops.md",
    "# 作物知识\n\n" + "\n".join(
        f"- **{c['name']}** (`{k}`): GDD={c['gdd_req']}, {c['seasons']}, buy={c['buy']} sell={c['sell']}"
        for k,c in sorted(CROPS.items())) +
    "\n\n## 链接\n[[/knowledge/strategy|策略]] | [[/state/farm|农场现状]]\n")
vwrite("knowledge/strategy.md",
    "# 农场策略\n\n"
    "1. 成熟→收获 2. 未水→浇水 3. 库存→出售 4. 空地+种子→种植\n"
    "5. 无地→开垦 6. 缺种子→买 7. 杂草→weed_all 8. 无事→过天\n\n"
    "## 链接\n[[/knowledge/crops|作物知识]] | [[/state/farm|农场现状]]\n")

# ═══════════════ GCG GRAPH ENGINE ═══════════════
def build_knowledge_graph():
    """Build DAG from vault — scans all .md files for [[wikilinks]]."""
    all_files = {}
    for root, dirs, fns in os.walk(VAULT):
        for fn in fns:
            if fn.endswith('.md'):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, VAULT).replace('\\', '/')
                all_files[rel] = full
    files = {}; edges = []; anchors = set()
    for rel, full in all_files.items():
        try:
            with open(full, 'r', encoding='utf-8') as fh:
                content = fh.read()
        except: content = ""
        links = re.findall(r'\[\[([^\]|#]+)(?:[|#][^\]]+)?\]\]', content)
        files[rel] = {'content': content, 'links': links,
            'is_anchor': rel.startswith('knowledge/'),
            'is_phenom': rel == 'state/farm.md'}
        if files[rel]['is_anchor']: anchors.add(rel)
        src_dir = os.path.dirname(rel)
        for target in links:
            tc = target.strip()
            resolved = os.path.normpath(os.path.join(src_dir, tc)).replace('\\', '/')
            if resolved in all_files: edges.append((resolved, rel))
            elif resolved + '.md' in all_files: edges.append((resolved + '.md', rel))
    return files, edges, anchors

def compute_gcg_metrics(files, edges, anchors):
    if not files or not edges:
        return {"phi": 0.0, "bridge_ratio": 0.0, "avg_g": 0.0, "eff_max": 0.0, "n_nodes": 0, "n_edges": 0}
    ni = {n: i for i, n in enumerate(sorted(files.keys()))}
    children = defaultdict(list); parents = defaultdict(list)
    for src, tgt in edges:
        if src in ni and tgt in ni:
            si, ti = ni[src], ni[tgt]
            children[si].append(ti); parents[ti].append(si)
    N = len(ni); total = len(edges)
    g = {}; q = deque()
    for nid, info in files.items():
        if info['is_anchor'] or info['is_phenom']:
            if nid in ni: g[ni[nid]] = 0; q.append((ni[nid], 0))
    while q:
        cur, d = q.popleft()
        for v in children.get(cur, []):
            if v not in g: g[v] = d + 1; q.append((v, d + 1))
    br = 0; avg_g = 0
    for nid in ni.values():
        pc = len(parents.get(nid, []))
        if pc == 1: br += 1
        avg_g += g.get(nid, N)
    br /= max(1, N)
    avg_g /= max(1, N)
    g_vals = [g.get(nid, N) for nid in ni.values()]
    phi = sum(1 for v in g_vals if v > avg_g + 3) / max(1, N) if g_vals else 0
    eff = {}
    for nid in ni.values():
        ch = children.get(nid, [])
        if not ch: eff[nid] = 0.0
        else:
            ev = 0.0
            for c in ch:
                din = len(parents.get(c, []))
                ev += (1.0 / max(1, din)) * (1.0 + eff.get(c, 0.0))
            eff[nid] = ev
    eff_max = max(eff.values()) if eff else 0.0
    return {"phi": phi, "bridge_ratio": br, "avg_g": avg_g, "eff_max": eff_max, "n_nodes": N, "n_edges": total}

# ═══════════════ VAULT HIERARCHY HELPERS ═══════════════
# ═══════════════ VAULT HIERARCHY: YEAR → SEASON → DAY ═══════════════
# Structure:
#   knowledge/crops.md ←→ knowledge/strategy.md ←→ state/farm.md (three pillars)
#   decisions/Y1/_index.md → 4 seasons → each season/_index.md → all day files
#   state/farm.md ← linked from both knowledge and decisions

def _day_path(year, season, day):
    return f"decisions/Y{year}/{season}/day{day:02d}.md"


def _write_vault(cycle, state, action, reason, gcg, vfy, ok, msg):
    """Core vault writer — called every cycle. Maintains year→season→day hierarchy."""
    y = state.get("year", 1); s = state.get("season", "?"); d = state.get("day", 0)
    dp = _day_path(y, s, d)

    # ── DAY FILE — append cycle entry ──
    br = gcg.get("bridge_ratio", 0)
    entry = f"\n### [{cycle}] {action} {'OK' if ok else 'FAIL'}\n"
    entry += f"- {reason} | G={state.get('gold',0)} E={state.get('energy',0)} BR={br:.3f}\n"
    entry += f"- {str(msg)[:120]}\n"

    existing = vread(dp)
    if not existing:  # first cycle of this day → write header
        header = (f"# {s} Day{d} Y{y} — {state.get('weather','?')}\n"
                  f"- G={state.get('gold',0)} E={state.get('energy',0)} "
                  f"T={state.get('tilled',0)} P={state.get('planted',0)}\n"
                  f"- [[../../_index|Y{y}]] | [[../_index|{s}季]] | "
                  f"[[../../../knowledge/crops|作物]] | [[../../../state/farm|农场现状]]\n\n")
        entry = header + entry
    vappend(dp, entry)

    # ── DAY OBSERVATIONS — add if notable ──
    obs = []
    for c in state.get("crops", []):
        if c.get("gdd_percent", 0) >= 95:
            obs.append(f"- 🟢 {c.get('crop_name',c.get('crop_type','?'))}")
    w = str(state.get("weather", ""))
    if w in ("stormy", "frost", "drought", "flood", "heat_wave"):
        obs.append(f"- ⚠ {w}")
    if len(state.get("storage", [])) > state.get("storage_capacity", 50) * 0.7:
        obs.append(f"- 📦 高库存 {len(state['storage'])}")
    if obs and "## 观测" not in existing:
        vappend(dp, "## 观测\n" + "\n".join(obs) + "\n")

    # ── SEASON INDEX — links all days ──
    si = (f"# {s} Y{y}\n"
          f"- G={state.get('gold',0)} E={state.get('energy',0)} | "
          f"T={state.get('tilled',0)} P={state.get('planted',0)}\n"
          f"- [[../../_index|Y{y}年]] | [[../../../knowledge/crops|作物]] | [[../../../state/farm|农场现状]]\n\n"
          f"## 天数\n")
    for day_num in range(1, 29):
        day_file = f"day{day_num:02d}"
        # Only link days that actually exist (check first day to avoid 28 links)
        si += f"- [[{day_file}|Day{day_num}]]\n" if day_num <= d else f"- Day{day_num}\n"
    vwrite(f"decisions/Y{y}/{s}/_index.md", si)

    # ── YEAR INDEX — connects 4 seasons + 3 core knowledge files ──
    seasons_done = sum(1 for sn in ["Spring","Summer","Fall","Winter"]
                       if os.path.exists(os.path.join(VAULT, f"decisions/Y{y}/{sn}/_index.md")))
    yi = (f"# Y{y}年\n\n"
          f"## 当前\n- 金币: {state.get('gold',0)}  分数: {state.get('score',0)}\n"
          f"- {s} Day{d}  {state.get('weather','?')}\n\n"
          f"## 四季\n"
          f"- [[Spring/_index|🌱 春 {s}]] ({'✓' if 'Spring' in ['Spring','Summer','Fall','Winter'][:seasons_done] else '…'})\n"
          f"- [[Summer/_index|☀ 夏]]\n"
          f"- [[Fall/_index|🍂 秋]]\n"
          f"- [[Winter/_index|❄ 冬]]\n\n"
          f"## 知识库\n"
          f"- [[../knowledge/crops|📖 作物知识]]\n"
          f"- [[../knowledge/strategy|🎯 农场策略]]\n"
          f"- [[../state/farm|📊 农场现状]]\n")
    vwrite(f"decisions/Y{y}/_index.md", yi)

    # ── STATE/FARM.MD — comprehensive real-time snapshot ──
    _write_farm_state(state)

    # ── JSONL log for Phase C ──
    vu.log_decision(VAULT, f"Y{y}C{cycle}", state, action, {}, ok, msg, gcg)
    vappend("knowledge/learned.md",
             f"- [{'✓' if ok else '✗'}] `{action}` G={state.get('gold',0)}: {str(msg)[:100]}\n")


def _write_farm_state(state):
    """Write comprehensive farm snapshot to state/farm.md.
    Three pillars: farm.md ↔ crops.md ↔ strategy.md — each links to the others."""
    s = state.get("season", "?"); d = state.get("day", 0); y = state.get("year", 1)
    farmer = state.get("farmer", {})
    storage = state.get("storage", [])
    buildings = state.get("buildings", [])
    crops = state.get("crops", [])
    inv = state.get("inventory", {})

    md = f"# 农场现状 — {s} Day{d} Y{y}\n\n"

    # Key metrics table
    md += f"| 指标 | 值 | 指标 | 值 |\n|------|------|------|------|\n"
    md += f"| 💰 金币 | {state.get('gold',0)} | ⚡ 体力 | {state.get('energy',0)} |\n"
    md += f"| 🏆 分数 | {state.get('score',0)} | 🌤 天气 | {state.get('weather','?')} |\n"
    md += f"| ⛏ 翻耕 | {state.get('tilled',0)} | 🌱 已种植 | {state.get('planted',0)} |\n"
    md += f"| 🌿 杂草 | {state.get('weed_count',0)} | 🏔 表土 | {state.get('avg_topsoil',0):.1f}cm |\n"
    # Tax computation (progressive)
    g = state.get('gold', 0)
    if g < 500: tax_rate = "0%" ; tax_g = 0
    elif g < 5000: tax_rate = "0.1%" ; tax_g = int(g * 0.001)
    elif g < 10000: tax_rate = "0.5%" ; tax_g = int(g * 0.005)
    elif g < 20000: tax_rate = "1%" ; tax_g = int(g * 0.01)
    else: tax_rate = "1.5%" ; tax_g = int(g * 0.015)
    md += f"| 📦 仓库 | {len(storage)}/{state.get('storage_capacity',50)} | 🏗 建筑 | {len(buildings)} |\n"
    md += f"| 🏛 日税 | {tax_rate} ({tax_g}G) | 💧 口渴 | {farmer.get('hydration','?')} |\n\n"

    # Body
    skills = farmer.get("skills", {})
    md += "## 🧑 农夫\n"
    md += f"| 体力 | 饥饿 | 口渴 | 疲劳 | 农耕 | 畜牧 | 机械 | 加工 |\n"
    md += f"|------|------|------|------|------|------|------|------|\n"
    md += f"| {state.get('energy',0)} | {farmer.get('hunger','?')} | {farmer.get('hydration','?')} | "
    md += f"{farmer.get('fatigue','?')} | {skills.get('farming','?')} | {skills.get('husbandry','?')} | "
    md += f"{skills.get('machinery','?')} | {skills.get('processing','?')} |\n\n"

    # Buildings
    md += "## 🏗 建筑\n"
    if buildings:
        for b in buildings:
            md += f"- {b}\n"
    else:
        md += "- 尚未建造任何建筑\n"
    md += "\n"

    # Crops
    md += f"## 🌾 作物 ({len(crops)}株)\n"
    if crops:
        for c in crops[:15]:
            gdd = c.get("gdd_percent", 0)
            icon = "🟢" if gdd >= 95 else "🟡" if gdd >= 50 else "🔵"
            water = "💧" if c.get("watered_today") else "🏜"
            cn = c.get('crop_name', c.get('crop_type', '?'))
            px, py = c.get("position_x", 0), c.get("position_y", 0)
            md += f"- {icon}{water} {cn} @({px},{py}) GDD={gdd}%\n"
        if len(crops) > 15:
            md += f"- ... 还有 {len(crops)-15} 株\n"
    else:
        md += "- 暂无作物种植\n"
    md += "\n"

    # Storage
    md += "## 📦 仓库\n"
    if storage:
        for item in storage[:8]:
            q = item.get("quality", "?")
            fresh = item.get("freshness_days", 0)
            max_f = item.get("max_freshness", 7)
            md += f"- {item.get('name','?')} [{q}级] {fresh}/{max_f}d\n"
        if len(storage) > 8:
            md += f"- ... 还有 {len(storage)-8} 项\n"
    else:
        md += "- 空空如也\n"
    md += "\n"

    # Seeds
    seeds = {k: v for k, v in inv.items() if v > 0 and "_seeds" in k}
    if seeds:
        md += "## 🌱 种子袋\n"
        for k, v in seeds.items():
            md += f"- {k}: {v}颗\n"
        md += "\n"

    # Links
    md += "## 🔗 链接\n"
    md += f"- [[/knowledge/crops|📖 作物知识]] | [[/knowledge/strategy|🎯 策略]]\n"
    md += f"- [[/decisions/Y{y}/_index|📅 Y{y}年记录]] | "
    md += f"[[/decisions/Y{y}/{s}/_index|📅 {s}季]]\n"
    vwrite("state/farm.md", md)

# ═══════════════ DECISION ENGINE ═══════════════
def decide(state):
    s = state; e = s["energy"]; g = s["gold"]
    inv = s["inventory"]; season = s["season"]; crops = s["crops"]
    phase = s.get("day_phase", "morning")
    max_a = s.get("day_actions", {}).get(phase, 5)
    used = s.get("day_actions_used", 0)

    # Phase check
    if used >= max_a:
        return ("next_day", {}, "phase over", None, None)

    # Body survival
    farmer = s.get("farmer", {})
    if farmer.get("fatigue", 0) >= 30:
        return ("sleep", {}, f"疲{farmer['fatigue']}", None, None)
    if farmer.get("hunger", 100) < 50 and len(s.get("storage", [])) > 0:
        return ("eat", {}, f"饿{farmer['hunger']}", None, None)
    if farmer.get("hydration", 100) < 50:
        return ("drink_water", {}, f"渴{farmer['hydration']}", None, None)

    # Build GCG
    files, edges, anchors = build_knowledge_graph()
    metrics = compute_gcg_metrics(files, edges, anchors)
    decs = []
    storage = s.get("storage", [])

    # P1: HARVEST
    for c in crops:
        if c.get("gdd_percent", 0) >= 95:
            decs.append(("harvest", {}, f"收{c.get('crop_name',c.get('crop_type','?'))}"))

    # P2: SELL (sell EARLY — before gold runs out!)
    if not decs and len(storage) > 0 and g < 5000:
        decs.append(("sell_storage", {}, f"卖库存(G={g})"))

    # P3: WATER
    if not decs:
        need = [c for c in crops if not c.get("watered_today", False)]
        if need and s.get("weather","") not in ("rainy","flood"):
            decs.append(("water", {"positions": [[cw.get("position_x",0),cw.get("position_y",0)] for cw in need]}, "浇水"))

    # P4: BUILD
    if not decs:
        bs = s.get("buildings", [])
        if "fence" not in bs and g >= 2100:
            decs.append(("build", {"building_type": "fence"}, "建围栏"))
        elif "well" not in bs and g >= 3100:
            decs.append(("build", {"building_type": "well"}, "建水井"))

    # P5: WEED
    if not decs and s.get("weed_count", 0) > 5 and e >= 30:
        decs.append(("weed_all", {}, f"除草({s['weed_count']}棵)"))

    # P6: PLANT (only if we actually have seeds AND empty land)
    if not decs:
        empty = s["tilled"] - s["planted"]
        if e >= 12 and empty > 0:
            for cn, ci in CROPS.items():
                sk = f"{cn}_seeds"
                have = inv.get(sk, 0)
                if season in ci["seasons"] and have > 0:
                    n = min(empty, have, 3)
                    # Spread across rows to avoid occupied tiles
                    positions = []
                    for i in range(n):
                        row = s["planted"] + i  # offset by already-planted count
                        col = row % 5
                        positions.append([col, row // 5])
                    decs.append(("plant", {"crop_type": cn, "positions": positions},
                                 f"种{n}x{ci['name']}"))
                    break

    # P7: TILL
    if not decs:
        if s["tilled"] == 0 and e >= 50:
            decs.append(("till", {"positions": [[0,0],[0,1],[0,2]]}, "开垦3块"))
        elif s["tilled"] < 9 and e >= 30 and g >= 200:
            decs.append(("till", {"positions": [[1,0],[1,1],[1,2]]}, f"扩至{s['tilled']+3}块"))

    # P8: BUY SEEDS (buy winter_seeds in winter, parsnip in spring, etc.)
    if not decs and e >= 10 and g >= 100:
        for cn, ci in CROPS.items():
            if season in ci["seasons"]:
                sk = f"{cn}_seeds"
                if inv.get(sk, 0) < 5 and g >= ci["buy"] * 3:
                    decs.append(("buy", {"item_type": cn, "quantity": 3}, f"买{ci['name']}籽"))
                    break

    # P9: BUY FOOD if hungry and no storage items
    if not decs and e >= 10 and g >= 50 and farmer.get("hunger", 100) < 60 and len(storage) == 0:
        decs.append(("buy", {"crop_type": "bread", "quantity": 2}, "买食物充饥"))

    # P10: NEXT DAY
    if not decs:
        decs.append(("next_day", {}, "过天"))

    action, params, reason = decs[0]
    # Bridge3 verification gate
    bridge = metrics["bridge_ratio"]
    vfy = {"triggered": False, "bridge_at_trigger": bridge}
    if bridge > 0.4:
        vfy["triggered"] = True
        issues = []
        if action == "plant" and params.get("crop_type","") in CROPS:
            if season not in CROPS[params["crop_type"]]["seasons"]:
                issues.append("季节不符")
        if action == "harvest" and not any(c.get("gdd_percent",0) >= 95 for c in crops):
            issues.append("无成熟作物")
        vfy["issues_found"] = issues; vfy["overridden"] = len(issues) > 0
        if issues:
            action, params, reason = ("next_day", {}, "BRIDGE3: 回退过天")
    return action, params, reason, metrics, vfy

# ═══════════════ MAIN LOOP ═══════════════
print("\n" + "="*60)
print("XURENWU BRAIN-BODY v6 — Year→Season→Day Vault")
print("="*60)

today_str = datetime.date.today().isoformat()
bridge_history = []
_last_day = -1
_last_season = ""

for cycle in range(1000):
    now = time.strftime("%H:%M:%S")
    print(f"\n--- C{cycle+1} ({now}) ---")

    # GET STATE
    r = requests.get(f"{BASE_FARM}/api/farm/{FID}/status", headers=HDRS, proxies=PROX, timeout=10)
    d = r.json().get("data", r.json())
    state = {
        "season": d.get("season","?"), "day": d.get("day",0), "year": d.get("year",1),
        "weather": d.get("weather","?"), "gold": d.get("gold",0),
        "energy": d.get("energy",{}).get("current",0),
        "score": d.get("score",0),
        "crops": d.get("crops",[]),
        "inventory": {i.get("key","?"): i.get("count",0) for i in d.get("inventory_items",[])},
        "tilled": d.get("land_status",{}).get("tilled",0),
        "planted": d.get("land_status",{}).get("planted",0),
        "day_phase": d.get("day_phase","morning"),
        "day_actions_used": d.get("day_actions_used",0),
        "day_actions": d.get("day_actions",{"morning":5,"afternoon":3,"evening":1}),
        "farmer": d.get("farmer",{}),
        "storage": d.get("storage",[]),
        "storage_capacity": d.get("storage_capacity",50),
        "buildings": d.get("buildings",[]),
        "weed_count": d.get("weed_count",0),
        "avg_topsoil": d.get("avg_topsoil", 20.0),
        "weather_notes": d.get("weather_notes",[]),
    }

    # Record observations when day changes
    if state["day"] != _last_day:
        _last_day = state["day"]

    # Track season change
    if state["season"] != _last_season and _last_season:
        vwrite(f"knowledge/history/Y{state.get('year',1)}-{_last_season}.md",
               f"# {_last_season}总结\n- 终G={state['gold']}\n- {state['weather']}\n")
    _last_season = state["season"]

    # DECIDE
    action, params, reason, gcg, vfy = decide(state)
    if gcg is None: gcg = {"bridge_ratio": 0.0, "phi": 0.0, "avg_g": 0.0, "eff_max": 0.0, "n_nodes": 0, "n_edges": 0}
    if vfy is None: vfy = {"triggered": False, "bridge_at_trigger": 0.0}
    bridge_history.append(gcg["bridge_ratio"])

    br_flag = " [BRIDGE3]" if vfy.get("triggered") else ""
    print(f"  DECIDE: {action} — {reason}{br_flag}")
    if gcg.get("n_nodes", 0) > 0:
        print(f"  GCG: φ={gcg['phi']:.3f} BR={gcg['bridge_ratio']:.3f} nodes={gcg['n_nodes']} edges={gcg['n_edges']}")

    # ACT
    if action == "next_day":
        r = requests.post(f"{BASE_FARM}/api/farm/{FID}/next-day",
            json={"agent_id": AID}, headers=HDRS, proxies=PROX, timeout=10)
    else:
        body = {"agent_id": AID, "action_type": action}; body.update(params)
        r = requests.post(f"{BASE_FARM}/api/farm/{FID}/action",
            json=body, headers=HDRS, proxies=PROX, timeout=10)
    resp = r.json()
    ok = resp.get("success", False)
    msg = resp.get("message", resp.get("action_result", str(resp)[:100]))
    safe_msg = str(msg).encode('ascii', errors='replace').decode('ascii')
    print(f"  RESULT: {'OK' if ok else 'FAIL'} -- {safe_msg[:120]}")

    # RECORD to year→season→day vault (observations, season index, year index, farm state)
    _write_vault(cycle, state, action, reason, gcg, vfy, ok, msg)

    # BAR every 5 cycles
    if cycle % 5 == 4:
        try:
            r = requests.post(f"{BASE_BAR}/api/v1/guestbook/entries",
                json={"content": f"C{cycle+1}. {state['season']}D{state['day']}. {msg[:60]}"},
                headers=HDRS, proxies=PROX, timeout=5)
        except: pass

# ═══ FINAL ═══
r = requests.get(f"{BASE_FARM}/api/farm/{FID}/status", headers=HDRS, proxies=PROX, timeout=10)
fd = r.json().get("data", r.json())
final_gold = fd.get("gold", 0)
br_mean = sum(bridge_history) / len(bridge_history)
br_max = max(bridge_history)

print(f"\n{'='*60}")
print(f"DONE. Y{fd.get('year',1)} {fd['season']} D{fd['day']} Gold={final_gold} Score={fd.get('score',0)}")
print(f"BR mean={br_mean:.4f} max={br_max:.4f}")

vwrite("state/bridge-report.md",
    f"# 桥接比 — Y{fd.get('year',1)} {fd['season']} D{fd['day']}\n"
    f"- mean={br_mean:.4f} max={br_max:.4f}\n- samples={len(bridge_history)}\n")
vwrite("state/farm.md",
    f"# 农场 — Y{fd.get('year',1)} {fd['season']} D{fd['day']}\n"
    f"- G={final_gold} Score={fd.get('score',0)} | T={fd.get('land_status',{}).get('tilled',0)} "
    f"P={fd.get('land_status',{}).get('planted',0)}\n"
    f"- Buildings: {fd.get('buildings',[])}\n- Weeds: {fd.get('weed_count',0)}\n")
