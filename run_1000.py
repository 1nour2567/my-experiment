"""
Phase A+B 1000-cycle integration test
=====================================
Starts server, registers agent, runs 1000 decision cycles.
"""
import subprocess, sys, time, json, re, random, os, datetime

PY = r"C:\Users\m1916\AppData\Local\Programs\Python\Python313\python.exe"
WORKSPACE = r"\\wsl.localhost\Ubuntu\home\m191augustus\.claude\workspace"
VAULT = r"C:\Users\m1916\agent-brain"

# Clean vault
for d in ["decisions","state","daily","logs","events","diary",
           "knowledge/observations","knowledge/learned"]:
    path = os.path.join(VAULT, d)
    if os.path.exists(path):
        for root, dirs, files in os.walk(path, topdown=False):
            for f in files:
                os.remove(os.path.join(root, f))
            for subd in dirs:
                os.rmdir(os.path.join(root, subd))
os.makedirs(os.path.join(VAULT, "state"), exist_ok=True)
os.makedirs(os.path.join(VAULT, "knowledge/observations"), exist_ok=True)
os.makedirs(os.path.join(VAULT, "knowledge/learned"), exist_ok=True)
os.makedirs(os.path.join(VAULT, "decisions"), exist_ok=True)
os.makedirs(os.path.join(VAULT, "knowledge/history"), exist_ok=True)
print("Vault cleaned, dirs created")

# Remove old save
save_file = os.path.join(WORKSPACE, "agent_world_save.json")
if os.path.exists(save_file):
    os.remove(save_file)

# Start server
print("Starting server...")
server = subprocess.Popen(
    [PY, "-u", "agent_world_local.py"],
    cwd=WORKSPACE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True, bufsize=1
)

# Wait for server to print ready
ready = False
start_time = time.time()
for line in server.stdout:
    print(f"  [SERVER] {line.rstrip()}")
    if "All 3 sites running" in line:
        ready = True
        break
    if time.time() - start_time > 15:
        break

if not ready:
    print("FAIL: Server didn't start")
    server.kill()
    sys.exit(1)

time.sleep(2)
print("Server ready. Starting 1000-cycle test...\n")

# Import requests now that server is up
import requests
BASE_WORLD = "http://127.0.0.1:8080"
BASE_FARM = "http://127.0.0.1:8081"
PROXIES = {"http": None, "https": None}

# Register agent
user = f"xr_1k_{int(time.time())%100000}"
r = requests.post(f"{BASE_WORLD}/api/agents/register",
    json={"username": user, "nickname": "Xu Renwu 1K", "bio": "1000-cycle Phase A+B test."},
    proxies=PROXIES, timeout=10)
d = r.json()
KEY = d["data"]["api_key"]
VC = d["data"]["verification"]["verification_code"]
CH = d["data"]["verification"]["challenge_text"]
nums = [int(n) for n in re.findall(r'[-]?\d+', CH)]
ans = nums[0] + nums[1] if '+' in CH else nums[0] - nums[1]
r = requests.post(f"{BASE_WORLD}/api/agents/verify",
    json={"verification_code": VC, "answer": str(ans)}, proxies=PROXIES, timeout=10)
assert r.json()["success"], "Verification failed"
AID = d["data"]["agent_id"]  # Use UUID from registration, not username
HDRS = {"agent-auth-api-key": KEY}

# Create farm
r = requests.post(f"{BASE_FARM}/api/farm/register",
    json={"agent_id": AID, "name": "Xu Renwu 1K"}, headers=HDRS, proxies=PROXIES, timeout=10)
FID = r.json().get("farm_id", "")
print(f"Agent: {AID}  Farm: {FID[:20]}...\n")

# Load crop config
r = requests.get(f"{BASE_FARM}/api/game/config", headers=HDRS, proxies=PROXIES, timeout=10)
CROPS = {c["crop_type"]: {
    "name": c.get("name", c["crop_type"]),
    "seasons": [s.strip() for s in c.get("seasons", "").split(",")],
    "gdd_req": c.get("gdd_required", 30), "buy": c.get("buy_price", 0),
    "sell": c.get("sell_price", 0),
} for c in r.json().get("crops", [])}
print(f"Loaded {len(CROPS)} crops\n")

# Decision engine with survival loops
def decide(state):
    """P0 survival → P1 harvest → P2 sell → P3 water → P4 plant → P5 expand → P6 buy → fallback"""
    s = state["season"]
    e = state["energy"]
    g = state["gold"]
    inv = state.get("inventory", {})
    crops = state.get("crops", [])
    tilled = state.get("tilled", 0)
    planted = state.get("planted", 0)
    phase = state.get("day_phase", "morning")
    actions_left = state.get("day_actions", {}).get(phase, 5) - state.get("day_actions_used", 0)

    if actions_left <= 0:
        return ("next_day", {}, "phase over")

    # Get farmer body state
    farmer = state.get("farmer", {})
    hunger = farmer.get("hunger", 100)
    hydration = farmer.get("hydration", 100)
    fatigue = farmer.get("fatigue", 0)

    # P0: SURVIVAL — body recovery (matching server thresholds)
    if fatigue >= 30:
        return ("sleep", {}, f"SLEEP fatig={fatigue}")
    if hunger < 50 and len(state.get("storage", [])) > 0:
        return ("eat", {}, f"EAT hung={hunger}")
    if hydration < 50:
        return ("drink_water", {}, f"DRINK hyd={hydration}")

    # P1: HARVEST mature crops
    for c in crops:
        if c.get("gdd_percent", 0) >= 95:
            return ("harvest", {}, f"HARVEST {c.get('crop_name','?')}")

    # P2: SELL if storage has items (always convert crops to gold)
    storage = state.get("storage", [])
    if len(storage) > 0 and g < 5000:
        return ("sell_storage", {}, f"SELL ({len(storage)} items, gold={g})")

    # P3: WATER crops that need it
    need_water = [c for c in crops if not c.get("watered_today", False)]
    if need_water and state["weather"] not in ("rainy", "flood") and e >= 10:
        return ("water", {"positions": [[c.get("position_x",0), c.get("position_y",0)] for c in need_water]}, "WATER")

    # P4: BUILD (if gold surplus)
    buildings = state.get("buildings", [])
    if "fence" not in buildings and g >= 2200:
        return ("build", {"building_type": "fence"}, "BUILD fence")
    if "well" not in buildings and g >= 3200:
        return ("build", {"building_type": "well"}, "BUILD well")
    if "root_cellar" not in buildings and g >= 5000:
        return ("build", {"building_type": "root_cellar"}, "BUILD root_cellar")

    # P5: CHECK contracts
    contracts = state.get("available_contracts", [])
    signed = state.get("signed_contracts", [])
    if contracts and not signed and e >= 2 and g >= 100 and g < 5000:
        return ("sign_contract", {"contract_id": contracts[0].get("id","")}, "SIGN CONTRACT")

    # P6: PLANT
    empty = tilled - planted
    if e >= 12 and empty > 0:
        for cn, ci in CROPS.items():
            seed_key = f"{cn}_seeds"
            if s in ci["seasons"] and inv.get(seed_key, 0) > 0:
                n = min(empty, inv[seed_key], 5)
                return ("plant", {"crop_type": cn, "positions": [[0,i] for i in range(n)]}, f"PLANT {n}x{ci['name']}")

    # P7: SAVE SEEDS if storage has S/A quality crops and we're about to run out
    storage = state.get("storage", [])
    if storage and g >= 100 and len(storage) > 5:
        high_q = [s for s in storage if s.get("quality") in ("S", "A")]
        if high_q:
            return ("save_seeds", {}, f"SAVE SEEDS ({len(high_q)} high-quality)")

    # P8: TILL
    if tilled == 0 and e >= 50:
        return ("till", {"positions": [[0,0],[0,1],[0,2]]}, "TILL 3")
    if tilled < 9 and e >= 30 and g >= 100:
        return ("till", {"positions": [[1,0],[1,1],[1,2]]}, f"TILL expand")

    # P9: BUY SEEDS
    if e >= 10 and g >= 150:
        for cn, ci in CROPS.items():
            if s in ci["seasons"]:
                seed_key = f"{cn}_seeds"
                have = inv.get(seed_key, 0)
                if have < 5 and g >= ci["buy"] * 3:
                    return ("buy", {"item_type": cn, "quantity": 3}, f"BUY {cn}")

    # P10: SLEEP if tired
    if fatigue > 60 and phase == "evening":
        return ("sleep", {}, f"SLEEP fatigue={fatigue}")

    return ("next_day", {}, "NEXT DAY")

# Main loop
log_lines = []
start = time.time()
farm_state = None
stats = {"harvests": 0, "plants": 0, "waters": 0, "buys": 0, "tills": 0,
         "next_days": 0, "sells": 0, "sleeps": 0, "eats": 0, "drinks": 0,
         "signs": 0, "builds": 0, "saves": 0,
         "failures": 0, "turns": 0}

for cycle in range(1000):
    # GET STATE
    try:
        r = requests.get(f"{BASE_FARM}/api/farm/{FID}/status", headers=HDRS, proxies=PROXIES, timeout=10)
        d = r.json().get("data", r.json())
        state = {
            "season": d.get("season", "?"), "day": d.get("day", 0),
            "weather": d.get("weather", "?"), "gold": d.get("gold", 0),
            "energy": d.get("energy", {}).get("current", 0),
            "crops": d.get("crops", []),
            "inventory": {i.get("key", "?"): i.get("count", 0) for i in d.get("inventory_items", [])},
            "tilled": d.get("land_status", {}).get("tilled", 0),
            "planted": d.get("land_status", {}).get("planted", 0),
            "day_phase": d.get("day_phase", "morning"),
            "day_actions_used": d.get("day_actions_used", 0),
            "day_actions": d.get("day_actions", {"morning": 5, "afternoon": 3, "evening": 1}),
            "storage": d.get("storage", []),
            "storage_capacity": d.get("storage_capacity", 50),
            "gdd_today": d.get("gdd_today", 10),
            "weed_count": d.get("weed_count", 0),
            "topsoil_warnings": d.get("topsoil_warnings", []),
            "weather_notes": d.get("weather_notes", []),
            "signed_contracts": d.get("signed_contracts", []),
            "available_contracts": d.get("available_contracts", []),
            "tool_status": d.get("tool_status", {}),
            "farmer": d.get("farmer", {}),
            "buildings": d.get("buildings", []),
        }
    except Exception as exc:
        time.sleep(1)
        continue

    # DECIDE
    action, params, reason = decide(state)
    stats["turns"] += 1

    # EXECUTE
    try:
        if action == "next_day":
            r = requests.post(f"{BASE_FARM}/api/farm/{FID}/next-day",
                json={"agent_id": AID}, headers=HDRS, proxies=PROXIES, timeout=15)
        else:
            body = {"agent_id": AID, "action_type": action}
            body.update(params)
            r = requests.post(f"{BASE_FARM}/api/farm/{FID}/action",
                json=body, headers=HDRS, proxies=PROXIES, timeout=15)
        resp = r.json()
        ok = resp.get("success", False)
        msg = resp.get("message", resp.get("action_result", "?"))
        if ok:
            if action == "harvest": stats["harvests"] += 1
            elif action == "plant": stats["plants"] += 1
            elif action == "water": stats["waters"] += 1
            elif action == "buy": stats["buys"] += 1
            elif action == "till": stats["tills"] += 1
            elif action == "next_day": stats["next_days"] += 1
            elif action == "sell_storage": stats["sells"] += 1
            elif action == "sleep": stats["sleeps"] += 1
            elif action == "eat": stats["eats"] += 1
            elif action == "drink_water": stats["drinks"] += 1
            elif action == "sign_contract": stats["signs"] += 1
            elif action == "build": stats["builds"] += 1
            elif action == "save_seeds": stats["saves"] += 1
        else:
            stats["failures"] += 1
    except Exception as exc:
        stats["failures"] += 1
        msg = str(exc)[:50]

    # Progress every 50 cycles
    if cycle % 50 == 0:
        elapsed = time.time() - start
        g = state.get("gold", 0)
        s = state.get("season", "?")
        d = state.get("day", 0)
        w = state.get("weather", "?")
        e = state.get("energy", 0)
        tp = f"{state.get('tilled',0)}/{state.get('planted',0)}"
        print(f"[C{cycle:4d}] {s}D{d} {w} G={g} E={e} T/P={tp} | "
              f"H:{stats['harvests']} W:{stats['waters']} P:{stats['plants']} "
              f"$:{stats['sells']} B:{stats['buys']} Z:{stats['sleeps']} "
              f"N:{stats['next_days']} F:{stats['failures']} | {elapsed:.1f}s")

    time.sleep(0.05)  # small delay to avoid hammering

# Final report
elapsed = time.time() - start
r = requests.get(f"{BASE_FARM}/api/farm/{FID}/status", headers=HDRS, proxies=PROXIES, timeout=10)
fd = r.json().get("data", r.json())

print(f"\n{'='*60}")
print(f"1000 CYCLES COMPLETE — {elapsed:.1f}s ({1000/elapsed:.1f} cycles/s)")
print(f"{'='*60}")
print(f"Season: Y{fd.get('year',1)} {fd['season']} Day{fd['day']}")
print(f"Gold: {fd['gold']}  Score: {fd.get('score',0)}  Energy: {fd.get('energy',{}).get('current',0)}")
print(f"Tilled: {fd['land_status']['tilled']}  Planted: {fd['land_status']['planted']}")
print(f"Storage: {len(fd.get('storage',[]))}/{fd.get('storage_capacity',50)}")
print(f"Weeds: {fd.get('weed_count',0)}  Topsoil: {fd.get('avg_topsoil',0):.1f}cm")
print(f"Buildings: {fd.get('buildings', [])}")
print(f"Weather: {fd['weather']}  Notes: {fd.get('weather_notes',[])[:3]}")
print(f"Actions: H:{stats['harvests']} W:{stats['waters']} P:{stats['plants']} "
      f"$:{stats['sells']} B:{stats['buys']} Z:{stats['sleeps']} E:{stats['eats']} "
      f"D:{stats['drinks']} T:{stats['tills']} N:{stats['next_days']} F:{stats['failures']}")

# Write summary
with open(os.path.join(VAULT, "state", "farm.md"), "w", encoding="utf-8") as f:
    f.write(f"# 农场 — 1000周期测试\n")
    f.write(f"- Y{fd.get('year',1)} {fd['season']} Day{fd['day']} {fd['weather']} | gold={fd['gold']} score={fd.get('score',0)}\n")
    f.write(f"- Energy: {fd.get('energy',{}).get('current',0)}  Tilled: {fd['land_status']['tilled']}  Planted: {fd['land_status']['planted']}\n")
    f.write(f"- Weeds: {fd.get('weed_count',0)}  Topsoil: {fd.get('avg_topsoil',0):.1f}cm\n")
    f.write(f"- Stats: {json.dumps(stats)}\n")
    f.write(f"- Runtime: {elapsed:.1f}s\n")

print(f"\nReport: {VAULT}/state/farm.md")

server.kill()
server.wait()
