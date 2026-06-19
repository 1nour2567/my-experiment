"""
XuRenwu LLM Agent — DeepSeek brain, Obsidian vault, local Agent World
======================================================================
Agent reads state from vault -> asks LLM what to do -> executes -> records.

Refactored 2026-06-19: split into 4 modules.
Usage: python agent-world-llm.py [--profile xu_renwu|old_wang|iron_lady]
"""
import sys as _sys, re, time, os, json, datetime, requests

# ═══════════════ CONFIG ═══════════════
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

DEEPSEEK_KEY = os.environ["DEEPSEEK_KEY"]
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
BASE_WORLD = "http://127.0.0.1:8080"
BASE_FARM  = "http://127.0.0.1:8081"
BASE_BAR   = "http://127.0.0.1:8082"
PROX = {"http": None, "https": None}

# ═══════════════ PROFILE SELECTION ═══════════════
_AGENT_PROFILE_ID = "xu_renwu"
for _i, _arg in enumerate(_sys.argv[1:], 1):
    if _arg == "--profile" and _i < len(_sys.argv) - 1:
        _AGENT_PROFILE_ID = _sys.argv[_i + 1]; break
    elif _arg.startswith("--profile="):
        _AGENT_PROFILE_ID = _arg.split("=", 1)[1]

VAULT = os.path.join(r"C:\Users\m1916\agent-brain", "agents", _AGENT_PROFILE_ID)
PARENT_VAULT = r"C:\Users\m1916\agent-brain"
for _sub in ["knowledge","decisions","state","history","memory","knowledge/reports","knowledge/learned"]:
    os.makedirs(os.path.join(VAULT, _sub), exist_ok=True)

# ═══════════════ IMPORTS (Phase modules) ═══════════════
import vault_utils as vu
import importance_scorer, interrupt_system
import agent_profile, skill_tree, knowledge_map
import book_engine, rumor_engine, trade_engine
import prompts, context_builder, decision_executor

# ═══════════════ LLM CALL ═══════════════

def ask_llm(system_prompt, user_message, max_tokens=1500, max_retries=3):
    """Call DeepSeek API with retry on failure."""
    last_error = None
    for attempt in range(max_retries):
        try:
            r = requests.post(DEEPSEEK_URL, json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                "max_tokens": max_tokens, "temperature": 0.3,
            }, headers={
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
                "Content-Type": "application/json"
            }, timeout=45)
            data = r.json()
            if r.status_code == 429 or (isinstance(data, dict) and data.get("error", {}).get("type") == "rate_limit"):
                wait = 5 * (2 ** attempt)
                if attempt == 0: print(f"  LLM RATE LIMITED, retrying in {wait}s...")
                time.sleep(wait); continue
            if "choices" in data:
                resp_text = data["choices"][0]["message"]["content"]
                if not resp_text or not resp_text.strip():
                    if attempt < max_retries - 1: time.sleep(3); continue
                    return None
                return resp_text
            if "error" in data:
                last_error = json.dumps(data["error"], ensure_ascii=False)[:200]
                time.sleep(3 * (attempt + 1)); continue
        except Exception as e:
            last_error = str(e)[:100]
            time.sleep(3 * (attempt + 1)); continue
    print(f"  LLM FAILED after {max_retries} retries: {last_error}")
    return None

def ask_llm_structured(system_prompt, user_message):
    """Ask LLM for a structured decision. Returns parsed dict or None."""
    resp = ask_llm(system_prompt, user_message)
    if not resp: return None
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', resp, re.DOTALL)
    if json_match: resp = json_match.group(1)
    brace_start = resp.find('{')
    if brace_start >= 0:
        depth = 0; brace_end = brace_start
        for i in range(brace_start, len(resp)):
            if resp[i] == '{': depth += 1
            elif resp[i] == '}':
                depth -= 1
                if depth == 0: brace_end = i + 1; break
        json_str = resp[brace_start:brace_end]
        try:
            d = json.loads(json_str)
            d.setdefault("thoughts", d.get("action", "?"))
            d.setdefault("reasoning", "ok")
            return d
        except Exception:
            try:
                fixed = json_str.replace("'", '"').replace('\n', ' ')
                d = json.loads(fixed)
                d.setdefault("thoughts", d.get("action", "?"))
                d.setdefault("reasoning", "fixed")
                return d
            except Exception:
                print(f"  JSON parse failed: {json_str[:200]}")
    print(f"  LLM JSON parse failed. Raw: {resp[:300]}")
    return None

# ═══════════════ VAULT HELPERS ═══════════════

def vwrite(path, content): vu.vwrite(VAULT, path, content)
def vread(path): return vu.vread(VAULT, path)
def vappend(path, content): vu.vappend(VAULT, path, content)
def search_vault(keyword, n=5): return vu.search_vault(VAULT, keyword, n)

# ═══════════════ MEMGPT MEMORY ═══════════════

_working_memory = """# 工作记忆
(Agent can write here via 'remember' action. Persists across cycles.)
"""
_last_recall_result = ""

def _memory_handle(action, params, state):
    """MemGPT agent-driven memory: handle remember/recall/forget actions."""
    global _working_memory, _last_recall_result
    if action == "remember":
        topic = params.get("topic", "general")
        content = params.get("content", "")
        if not content: return "❌ remember needs 'content' field"
        mem_file = f"knowledge/learned/{topic}.md"
        existing = vread(mem_file)
        entry = f"\n### {state.get('season','?')}D{state.get('day',0)} Y{state.get('year',1)}\n{content}\n"
        vwrite(mem_file, (existing or f"# {topic} (agent memory)\n") + entry)
        _working_memory = f"# 工作记忆\n最近remember: {topic} — {content[:100]}\n"
        return f"🧠 记住了! {topic}: {content[:120]}"
    elif action == "recall":
        topic = params.get("topic", "")
        if not topic: return "❌ recall needs 'topic' field"
        results = search_vault(topic, 3)
        if results:
            lines = [f"## 🔍 recall: {topic}"]
            for rel, content in results: lines.append(f"### {rel}\n{content[:600]}")
            _last_recall_result = "\n".join(lines)
            return f"🔍 recall '{topic}': 找到{len(results)}条相关记录。"
        return f"❌ 没有关于'{topic}'的记忆。"
    elif action == "forget":
        topic = params.get("topic", "")
        if not topic: return "❌ forget needs 'topic' field"
        mem_file = f"knowledge/learned/{topic}.md"
        if os.path.exists(os.path.join(VAULT, mem_file)):
            content = vread(mem_file)
            if not content.startswith("[STALE]"): vwrite(mem_file, "[STALE] " + content)
            return f"🗑 标记为过期: {topic}"
        return f"❌ 没有名为'{topic}'的记忆"
    return ""

def _memory_retrieve(state):
    """Retrieve relevant memories for injection into context."""
    keywords = [state.get("season", "?"), state.get("weather", "?")]
    ctypes = set(c.get("crop_type","") for c in state.get("crops",[]))
    keywords.extend(list(ctypes)[:3])
    if state.get("gold", 0) < 500: keywords.append("buy")
    if state.get("weed_count",0) > 5: keywords.append("weeds")
    if any(c.get("gdd_percent",0) >= 90 for c in state.get("crops",[])): keywords.append("harvest")
    memory_matches = []
    for root, _, fns in os.walk(os.path.join(VAULT, "memory")):
        for fn in fns:
            if fn.endswith('.md') and fn != "plans.md":
                fp = os.path.join(root, fn)
                try:
                    with open(fp, 'r', encoding='utf-8') as f:
                        content = f.read()
                    for kw in keywords:
                        if kw.lower() in content.lower():
                            memory_matches.append((fn, content[:500])); break
                except Exception: pass
    if memory_matches:
        lines = ["## 🧠 相关记忆"]
        for name, content in memory_matches[:3]:
            lines.append(f"### {name}\n{content[:400]}")
        return "\n".join(lines)
    return ""

def _memory_write_plan(state, action, ok):
    """Append one line to the daily plan log."""
    line = f"[{datetime.datetime.now().strftime('%H:%M')}] {action}({'OK' if ok else 'FAIL'}) {state.get('season','?')}D{state.get('day',0)} E={state.get('energy',0)} G={state.get('gold',0)}\n"
    vappend("memory/plans.md", line)

def _memory_consolidate(state, cycle):
    """Every 50 cycles, compress working memory into a consolidated summary."""
    if cycle % 50 != 0: return
    recent_plans = vread("memory/plans.md")
    if not recent_plans or len(recent_plans.strip()) < 100: return
    recent_decisions = vu.load_recent_decisions(VAULT, 20)
    decision_text = "\n".join(
        f"[{d.get('season','?')}D{d.get('day',0)}] {d.get('action','?')}: {'OK' if d.get('success') else 'FAIL'}"
        for d in recent_decisions
    )
    consolidate_prompt = f"""You are compressing daily agent actions into learned knowledge.
Recent actions and decisions:
{decision_text}

Working plan log (last 50 cycles):
{recent_plans[-3000:]}

Output ONE line, no markdown. Focus on: what patterns emerged, what should be remembered."""
    summary = ask_llm("Summarize concisely. One sentence max.", consolidate_prompt, max_tokens=150)
    if summary and len(summary.strip()) > 10:
        _working_memory = f"# 工作记忆 (C{cycle})\n{summary.strip()}\n"
        vwrite("knowledge/learned/_consolidated.md",
               (vread("knowledge/learned/_consolidated.md") or "# 压缩记忆\n") + f"\n### C{cycle}\n{summary.strip()}\n")
        learned_file = f"knowledge/learned/C{cycle}-{state.get('season','?')}D{state.get('day',0)}.md"
        vwrite(learned_file, f"# C{cycle} 压缩\n{summary.strip()}\n")
        print(f"  [CONSOLIDATE] {summary.strip()[:150]}")

# ═══════════════ SEASON REPORTS ═══════════════

_last_report_season = ""

def _season_report(state):
    """Generate a season-end report from JSONL decision log."""
    global _last_report_season
    seasons = ["Spring","Summer","Fall","Winter"]
    s = state.get("season","?"); idx = seasons.index(s) if s in seasons else 0
    prev_idx = (idx - 1) % 4; prev_season = seasons[prev_idx]
    prev_year = state.get("year",1) - 1 if prev_idx == 3 else state.get("year",1)
    key = f"{prev_year}-{prev_season}"
    if key == _last_report_season: return
    _last_report_season = key
    entries = vu.load_decisions_for_season(VAULT, prev_season, prev_year)
    if len(entries) < 5: return
    total = len(entries)
    successes = sum(1 for e in entries if e.get("success"))
    sells = sum(1 for e in entries if e.get("action") == "sell_storage")
    harvests = sum(1 for e in entries if e.get("action") == "harvest" and e.get("success"))
    final_gold = 0
    try:
        final_gold = entries[-1].get("context_before", {}).get("gold", 0) if entries else 0
    except Exception: pass
    report = (
        f"# 📊 {prev_season} Y{prev_year} 总结\n\n"
        f"## 统计\n{total}条决策 | {successes}/{total-total+successes}成功 | {harvests}次收获 | {sells}次出售\n"
        f"季末金币: ~{final_gold}G\n"
    )
    report_path = f"knowledge/reports/Y{prev_year}-{prev_season}.md"
    vwrite(report_path, report)
    vwrite(f"history/Y{prev_year}-{prev_season}-report.md", report)
    print(f"  [REPORT] {prev_season} Y{prev_year}: {harvests} harvests, {sells} sales, ~{final_gold}G")

_last_past_report = ""

def _inject_past_season_report(state):
    """Return the past-year same-season report for cross-year learning."""
    s = state.get("season","?"); y = state.get("year",1)
    prev_report = f"knowledge/reports/Y{y-1}-{s}.md"
    content = vread(prev_report)
    if content: return f"## 📊 去年{s}的总结\n{content[:800]}\n"
    return ""

# ═══════════════ STATE PERSISTENCE ═══════════════

def _day_path(year, season, day):
    """Get the vault path for a given day's decision log."""
    return f"decisions/Y{year}/{season}/day{day:02d}.md"

def _write_vault_llm(cycle, state, action, result_msg, ok, thoughts, lesson):
    """Write agent decisions and state to the Obsidian vault."""
    today = datetime.date.today().isoformat()
    s = state.get("season","?"); d = state.get("day",0); y = state.get("year",1)
    entry =  f"- **{today} C{cycle+1} {s}D{d} Y{y}**: `{action}`\n"
    entry += f"- 体力={state['energy']} | 金币={state['gold']}\n"
    entry += f"- 想法: {thoughts[:200]}\n"
    if lesson: entry += f"- 学到了: {lesson[:200]}\n"
    entry += f"- 结果: {str(result_msg)[:120]}\n"
    entry += f"- {state['weather']} | 作物: {len(state['crops'])}株\n\n"
    dp = _day_path(y, s, d)
    vappend(dp, entry)
    # Write _index
    vwrite(f"decisions/Y{y}/{s}/_index.md",
        f"# Y{y} {s}\n共{28}天\n最近更新: C{cycle+1}\n")
    vwrite(f"decisions/Y{y}/_index.md",
        f"# Y{y}年决策索引\n最近季节: {s}\n最近更新: C{cycle+1}\n")

# ═══════════════ REGISTER ═══════════════

_prof = agent_profile.load_agent_profile(_AGENT_PROFILE_ID, PARENT_VAULT)
print(f"AGENT: {_prof.display_name} ({_prof.role}) — {_prof.bio[:50]}")
print(f"  Traits: industrious={_prof.fixed_traits.get('industriousness',0):.1f} curiosity={_prof.fixed_traits.get('curiosity',0):.1f}")
print(f"  Risk tolerance: {_prof.get_effective_risk_tolerance():.2f} | Perception bias: {_prof.perception_bias}")
_skill_summary = ", ".join(f"{k}={v.get('level',0)}" for k,v in sorted(_prof.skills.items()) if v.get("level",0)>0)
print(f"  Skills: {_skill_summary or 'none'}")

_SKILL_TREE = skill_tree.SkillTree.load(PARENT_VAULT, _prof.role)
print(f"  Skill Tree: {_SKILL_TREE.display_name} ({len(_SKILL_TREE.nodes)} nodes, {len(_SKILL_TREE.unlocked_nodes)} unlocked)")

_AGENT_PERSONA = _prof.personality_snippet()
_AGENT_SKILL_SUMMARY = _SKILL_TREE.get_skill_summary(
    _prof.get_skill_level("farming"), _prof.skills.get("farming",{}).get("xp",0))

_BOOK_ENGINE = book_engine.BookEngine(PARENT_VAULT)
_BOOK_LIBRARY = _BOOK_ENGINE.get_library(_prof.id)
if not _BOOK_LIBRARY:
    _BOOK_ENGINE.add_book_to_library(_prof.id, "wheat_guide", cycle=0)
    _BOOK_LIBRARY = _BOOK_ENGINE.get_library(_prof.id)
    print(f"  Starter book: wheat_guide added to library")
_RUMOR_ENGINE = rumor_engine.RumorEngine()
_TRADE_ENGINE = trade_engine.TradeEngine(PARENT_VAULT)

_synthesizer = importance_scorer.MemorySynthesizer(VAULT)

user = f"xr_{_prof.id}_{int(time.time())%10000}"
print(f"Registering {user} ({_prof.display_name})...")
r = requests.post(f"{BASE_WORLD}/api/agents/register",
    json={"username": user, "nickname": _prof.display_name, "bio": _prof.bio}, proxies=PROX, timeout=10)
d = r.json()
KEY = d["data"]["api_key"]
VC  = d["data"]["verification"]["verification_code"]
CH  = d["data"]["verification"]["challenge_text"]
nums = [int(n) for n in re.findall(r'[-]?\d+', CH)]
ans = nums[0] + nums[1] if '+' in CH else nums[0] - nums[1]
r = requests.post(f"{BASE_WORLD}/api/agents/verify",
    json={"verification_code": VC, "answer": str(ans)}, proxies=PROX, timeout=10)
assert r.json()["success"]
HDRS = {"agent-auth-api-key": KEY}; AID = user

# ── RESUME or CREATE farm ──
FID = None
try:
    r = requests.get(f"{BASE_FARM}/api/farms", headers=HDRS, proxies=PROX, timeout=5)
    for f in r.json().get("farms", []):
        if f.get("agent_name", "") == user:
            FID = f["farm_id"]
            print(f"RESUMING farm {FID[:20]}... (S{f['season']} D{f['day']} G{f['gold']})"); break
except Exception: pass
if not FID:
    r = requests.post(f"{BASE_FARM}/api/farm/register",
        json={"agent_id": AID, "name": f"{_prof.display_name} ({_prof.role})"}, headers=HDRS, proxies=PROX, timeout=10)
    FID = r.json().get("farm_id", "")
    print(f"NEW farm: {FID[:20]}...")
else:
    print(f"Farm: {FID[:20]}...")

# ── Load crop config ──
r = requests.get(f"{BASE_FARM}/api/game/config", headers=HDRS, proxies=PROX, timeout=10)
CROPS = {c["crop_type"]: {
    "name": c.get("name", c["crop_type"]),
    "seasons": [s.strip() for s in c.get("seasons", "").split(",")],
    "gdd_req": c.get("gdd_required", 30), "buy": c.get("buy_price", 25), "sell": c.get("sell_price", 50),
    "varieties": c.get("varieties", {"standard": {"traits": "?"}}),
} for c in r.json()["crops"]}

# ═══════════════ SYSTEM PROMPT ═══════════════
_SYSTEM_PROMPT = prompts.build_system_prompt(_AGENT_PERSONA, _AGENT_SKILL_SUMMARY)

# ═══════════════ MAIN LOOP ═══════════════
print("="*60)
print("XURENWU LLM AGENT (DeepSeek)")
print("="*60)

today = datetime.date.today().isoformat()
log_lines = []; lookup_result = ""
_last_failed = {"action": None, "count": 0}
_same_day_cycles = 0
_prev_day_key = None
mem_uses = {"remember": 0, "recall": 0, "forget": 0}

def find_decisions(vault_root):
    d = os.path.join(vault_root, "decisions")
    if not os.path.isdir(d): return []
    result = []
    for root, _, fns in os.walk(d):
        for fn in fns:
            if fn.endswith('.md'): result.append(fn)
    return result

for cycle in range(3000):
    now = time.strftime("%H:%M:%S")
    cid = f"{today}-llm-{cycle+1}"
    print(f"\n--- CYCLE {cycle+1} ({now}) ---")

    # ── 1. GET STATE ──
    r = requests.get(f"{BASE_FARM}/api/farm/{FID}/status", headers=HDRS, proxies=PROX, timeout=10)
    d = r.json().get("data", r.json())
    state = {
        "season": d.get("season","?"), "day": d.get("day",0), "year": d.get("year",1),
        "weather": d.get("weather","?"), "gold": d.get("gold",0), "score": d.get("score",0),
        "energy": d.get("energy",{}).get("current",0),
        "crops": d.get("crops",[]),
        "inventory": {i.get("key","?"): i.get("count",0) for i in d.get("inventory_items",[])},
        "tilled": d.get("land_status",{}).get("tilled",0),
        "planted": d.get("land_status",{}).get("planted",0),
        "soil_history": d.get("soil_history",{}),
        "day_phase": d.get("day_phase","morning"),
        "day_actions_used": d.get("day_actions_used",0),
        "day_actions": d.get("day_actions",{}),
        "hour": d.get("hour",7.0),
        "season_daylight": d.get("season_daylight",(6,20)),
        "is_night": d.get("is_night",False),
        "gdd_today": d.get("gdd_today",10),
        "npk": d.get("npk_summary",{}),
        "buildings": d.get("buildings",[]),
        "building_options": d.get("building_options",{}),
        "saved_seeds": d.get("saved_seeds",{}),
        "storage": d.get("storage",[]),
        "storage_summary": d.get("storage_summary",""),
        "storage_capacity": d.get("storage_capacity",50),
        "suggestions": d.get("suggestions",[]),
        "sensory_observations": d.get("sensory_observations",[]),
        "farmer": d.get("farmer",{}),
        "livestock": d.get("livestock",[]),
        "manure_stockpile": d.get("manure_stockpile",0),
        "water_sources": d.get("water_sources",[]),
        "weed_count": d.get("weed_count",0),
        "avg_topsoil": d.get("avg_topsoil",20.0),
        "weather_notes": d.get("weather_notes",[]),
        "frost_warning": d.get("frost_warning",False),
        "available_contracts": d.get("available_contracts",[]),
        "signed_contracts": d.get("signed_contracts",[]),
        "ecology_observations": d.get("ecology_observations",[]),
        "wolf_warning": d.get("wolf_warning",""),
        "ecology_alerts": d.get("ecology_alerts",[]),
    }

    # ── Day change detection ──
    current_day_key = f"{state.get('year',1)}-{state.get('season','?')}-{state.get('day',0)}"
    if _prev_day_key is not None and current_day_key != _prev_day_key:
        interrupt_system.reset_cooldowns()
        _same_day_cycles = 0
        old_parts = _prev_day_key.split("-")
        if len(old_parts) == 3:
            try:
                old_y, old_s, old_d = int(old_parts[0]), old_parts[1], int(old_parts[2])
                synth_result = _synthesizer.synthesize_day(old_s, old_y, old_d)
                if synth_result.get('high_importance_count', 0) > 0:
                    print(f"  [MEM-SYNTH] {old_s}D{old_d} Y{old_y}: {synth_result['high_importance_count']} important events scored ({synth_result['scored_count']} total)")
                    summary_path = f"history/Y{old_y}-{old_s}-D{old_d:02d}-synthesis.md"
                    vwrite(summary_path, synth_result.get('summary',''))
            except Exception as e:
                print(f"  [MEM-SYNTH WARN] {str(e)[:100]}".encode('ascii','replace').decode('ascii'))
    _prev_day_key = current_day_key

    # ── Same-day guard ──
    _same_day_cycles += 1
    _guard_triggered = _same_day_cycles >= 40

    # ── State persistence ──
    try:
        fm = state.get("farmer",{})
        st = state.get("storage",[])
        bd = state.get("buildings",[])
        md  = f"# 农场现状 — {state['season']} Day{state['day']} Y{state.get('year',1)}\n\n"
        md += f"> 📅 **{state['season']} 第{state['day']}天 第{state.get('year',1)}年** | 🕐 **{state['hour']:.0f}:00** | 🌤 **{state['weather']}**\n\n"
        md += f"| 指标 | 值 | 指标 | 值 |\n|------|------|------|------|\n"
        md += f"| 💰 金币 | {state['gold']} | ⚡ 体力 | {state['energy']} |\n"
        md += f"| 🏆 分数 | {state['score']} | 🌤 天气 | {state['weather']} |\n"
        g = state['gold']
        tax_rate,tax_g = ("0%",0) if g<500 else ("0.1%",int(g*0.001)) if g<5000 else ("0.5%",int(g*0.005)) if g<10000 else ("1%",int(g*0.01)) if g<20000 else ("1.5%",int(g*0.015))
        md += f"| 📦 仓库 | {len(st)}/{state.get('storage_capacity',50)} | 🏗 建筑 | {len(bd)} |\n"
        md += f"| 🏛 日税 | {tax_rate}({tax_g}G) | 💧 | {fm.get('hydration','?')} |\n\n"
        md += "## 🧑 农夫\n"
        skills = fm.get("skills",{})
        md += f"| 体力 | 饥饿 | 口渴 | 疲劳 | 睡意 | 农耕 | 畜牧 |\n"
        md += f"|------|------|------|------|------|------|------|\n| {state.get('energy',0)} | {fm.get('hunger','?')} | {fm.get('hydration','?')} | {fm.get('fatigue','?')} | {fm.get('sleepiness',0):.1f} | {skills.get('farming','?')} | {skills.get('husbandry','?')} |\n\n"
        crops = state.get("crops",[])[:15]
        if crops:
            md += "## 🌾 作物\n"
            for c in crops:
                gdd = c.get("gdd_percent",0)
                icon = "🟢" if gdd>=95 else ("🟡" if gdd>=50 else "🔴")
                wat = "💧" if c.get("watered_today") else "🏜"
                md += f"- {icon}{wat} {c.get('crop_name',c.get('crop_type','?'))} @({c.get('position_x',0)},{c.get('position_y',0)}) GDD={gdd}%\n"
            if len(crops)>15: md += f"- ... +{len(crops)-15}株\n"
        md += f"\n## 📦 仓库 ({len(st)}/{state.get('storage_capacity',50)})\n"
        for item in st[:8]:
            md += f"- {item.get('key',item.get('name','?'))} ×{item.get('count',1)}\n"
        seeds = {k:v for k,v in state.get('inventory',{}).items() if v>0 and '_seeds' in k}
        if seeds:
            md += "\n## 🌱 种子袋\n"
            for k,v in sorted(seeds.items()): md += f"- {k}: {v}颗\n"
        md += "\n## 🔗 链接\n- [[/knowledge/crops|📖 作物知识]] | [[/knowledge/strategy|🎯 策略]]\n"
        md += f"- [[/decisions/Y{state.get('year',1)}/_index|📅 Y{state.get('year',1)}年记录]]\n"
        vwrite("state/farm.md", md)
    except Exception: pass

    # ── 3. BUILD CONTEXT ──
    ctx = context_builder.build_user_message(state,
        cycle=cycle, log_lines=log_lines, lookup_result=lookup_result,
        _prof=_prof, PARENT_VAULT=PARENT_VAULT, VAULT=VAULT,
        interrupt_context={}, _same_day_cycles=_same_day_cycles,
        CROPS=CROPS, _last_past_report=_last_past_report,
        _last_failed=_last_failed, _BOOK_ENGINE=_BOOK_ENGINE,
        _RUMOR_ENGINE=_RUMOR_ENGINE, _TRADE_ENGINE=_TRADE_ENGINE,
        _AGENT_PROFILE_ID=_AGENT_PROFILE_ID, _memory_retrieve=_memory_retrieve,
        vread=vread, agent_profile=agent_profile, knowledge_map=knowledge_map,
        HDRS=HDRS, PROX=PROX, BASE_FARM=BASE_FARM, BASE_BAR=BASE_BAR,
        FID=FID, AID=AID)
    user_msg = ctx["user_msg"]
    lookup_result = ctx.get("lookup_result","")
    _last_past_report = ctx.get("_last_past_report","")
    user_msg += "\n→ Output valid JSON. Keep thoughts < 80 chars."

    # ── Same-day warning ──
    if 25 <= _same_day_cycles < 40:
        user_msg = "⚠ 你已经在这天停留了" + str(_same_day_cycles) + "个周期。有成熟作物就harvest → 有种子就plant → 无事可做就next_day。\n" + user_msg

    # ── 4. CHECK INTERRUPTS ──
    interrupt_context = interrupt_system.check_interrupts(state, {"last_failed": _last_failed, "cycle": cycle})
    if interrupt_context["active"]:
        user_msg = interrupt_context["header"] + user_msg
        print(f"  [INTERRUPT] {', '.join(t['name'] for t in interrupt_context['fired'])}")

    # ── 5. ASK LLM ──
    decision = ask_llm_structured(_SYSTEM_PROMPT, user_msg)
    if decision is None:
        print(f"  LLM failed — fallback engine")
        decision = decision_executor.fallback_decision(state, CROPS)

    action = decision.get("action", "next_day")
    params = decision.get("params", {}) or {}
    reasoning = decision.get("reasoning", "")
    thoughts = decision.get("thoughts", "")

    if _guard_triggered:
        action = "next_day"; params = {}
        reasoning = "forced next_day: too many cycles on same day"
        _same_day_cycles = 0
        print(f"  [GUARD] forcing next_day (40+ cycles on same day)")

    params = decision_executor.normalize_params(params)

    print(f"  THOUGHTS: {thoughts[:200]}")
    print(f"  DECIDE: {action} par={json.dumps(params,ensure_ascii=True)[:80]} -- {reasoning[:150]}")

    # ── 6. EXECUTE ──
    exec_result = decision_executor.execute_action(action, params, state,
        cycle=cycle, _prof=_prof, _SKILL_TREE=_SKILL_TREE, VAULT=VAULT,
        PARENT_VAULT=PARENT_VAULT, FID=FID, AID=AID, HDRS=HDRS, PROX=PROX,
        BASE_FARM=BASE_FARM, BASE_BAR=BASE_BAR, _BOOK_ENGINE=_BOOK_ENGINE,
        _RUMOR_ENGINE=_RUMOR_ENGINE, _TRADE_ENGINE=_TRADE_ENGINE,
        _AGENT_PROFILE_ID=_AGENT_PROFILE_ID, vread=vread, vwrite=vwrite,
        _memory_handle=_memory_handle, mem_uses=mem_uses,
        _same_day_cycles=_same_day_cycles, log_lines=log_lines)
    result_msg = exec_result["result_msg"]
    ok = exec_result["ok"]
    if exec_result.get("lookup_result"):
        lookup_result = exec_result["lookup_result"]

    # ── Repeated failure guard ──
    result_msg_safe = str(result_msg).encode('ascii', errors='replace').decode('ascii')
    print(f"  RESULT: {'OK' if ok else 'FAIL'} -- {result_msg_safe[:120]}")
    if not ok:
        if _last_failed["action"] == action:
            _last_failed["count"] += 1
        else:
            _last_failed = {"action": action, "count": 1}
        if _last_failed["count"] >= 3:
            print(f"  GUARD: {action} failed {_last_failed['count']}x — forcing next_day")
            action = "next_day"; params = {}; ok = True; result_msg_safe = "forced next_day"; result_msg = "forced"
            _last_failed = {"action": None, "count": 0}
    else:
        _last_failed = {"action": None, "count": 0}

    # ── 7. ACTION CHAINING ──
    if ok:
        if action == "buy":
            lookup_result = "✅ 种子已购买！下一个动作必须是 till 翻耕——开垦和种子数量相同的土地。"
        elif action in ("till", "till_bulk"):
            lookup_result = "✅ 土地已翻耕！下一个动作必须是 plant 播种——选择你有种子的作物种下去。"
        elif action in ("plant", "plant_bulk"):
            lookup_result = "✅ 种子已种下！下一个动作必须是 water 浇水——每天都需要浇灌。"
        elif action == "water":
            ripe = [c for c in state.get('crops',[]) if c.get('gdd_percent',0) >= 95]
            lookup_result = "✅ 已浇水。有作物成熟了——立即 harvest 收获！" if ripe else "✅ 已浇水。检查：还有空地和种子吗？继续 plant；没有就 next_day。"

    # ── 8. LEARN FROM FAILURES ──
    lesson = ""
    if not ok and action not in ("next_day","lookup","remember","recall","forget","sleep","eat","drink_water"):
        learn_prompt = f"""You are an AI learning from mistakes. What went wrong and what should be done differently?
Action: {action} FAILED. Error: {result_msg}
Current season: {state['season']} Day{state['day']}. Gold={state['gold']}
Output JSON: {{"lesson": "<what to remember>", "why_failed": "<root cause>", "prevention": "<how to avoid>"}}"""
        lesson_json = ask_llm_structured("Learn from failures. Output valid JSON.", learn_prompt)
        if lesson_json:
            lesson = lesson_json.get("lesson","")
            why = lesson_json.get("why_failed","")
            prevention = lesson_json.get("prevention","")
            err_entry = f"\n- **{today} {now}**: `{action}`失败 — {lesson}\n  - 原因: {why}\n  - 预防: {prevention}\n"
            existing = vread("knowledge/errors.md")
            if lesson not in existing:
                vwrite("knowledge/errors.md", existing + err_entry)
                print(f"  LEARNED: {lesson[:150]}")
                log_lines.append(f"[{now}] LEARNED: {lesson[:100]}")
                # Compress errors if >200KB
                try:
                    fsize = os.path.getsize(os.path.join(VAULT,"knowledge","errors.md"))
                    if fsize > 200_000:
                        parts = existing.split("\n- **")
                        header = parts[0] if parts else ""
                        entries = ["- **" + p for p in parts[1:]]
                        kept = entries[-200:]
                        vwrite("knowledge/errors.md", header + f"\n\n> 📦 压缩于 {now}: 保留{len(kept)}条\n" + "".join(kept))
                        vwrite(f"history/errors-archive-{today}.md", header + "\n".join(entries[:-200]))
                        print(f"  [COMPRESS] errors.md: {len(entries)}->{len(kept)} entries")
                except Exception: pass

    # ── 9. RECORD ──
    _write_vault_llm(cycle, state, action, result_msg, ok, thoughts, lesson)
    _memory_write_plan(state, action, ok)
    _memory_consolidate(state, cycle)

    # ── Season report ──
    current_season = f"{state.get('year',1)}-{state.get('season','?')}"
    if current_season != _last_report_season:
        _season_report(state)
        _last_report_season = current_season
        try:
            seasons = ["Spring","Summer","Fall","Winter"]
            s = state.get("season","?"); idx = seasons.index(s) if s in seasons else 0
            prev_idx = (idx - 1) % 4; prev_season = seasons[prev_idx]
            prev_year = state.get("year",1) - 1 if prev_idx == 3 else state.get("year",1)
            reflection = _synthesizer.generate_reflection(prev_season, prev_year)
            if reflection:
                vwrite(f"knowledge/learned/reflection-Y{prev_year}-{prev_season}.md", reflection)
                print(f"  [REFLECTION] {prev_season} Y{prev_year}: stored to knowledge/learned/")
        except Exception as e:
            print(f"  [REFLECTION WARN] {str(e)[:80]}".encode('ascii','replace').decode('ascii'))
    past_report = _inject_past_season_report(state)
    if past_report and "## 📊 去年" in past_report:
        _last_past_report = past_report

    extra = ""
    if action == "next_day" and resp.get("gdd_today"):
        extra = f" GDD={resp['gdd_today']} crops={resp.get('crop_gdd',[])}"
    log_lines.append(f"[{now}] {action}: {result_msg[:100]}{extra}")

    if action == "next_day" and ok: _same_day_cycles = 0
    vu.log_decision(VAULT, cid, state, action, params, ok, result_msg)

    # XP + personality drift
    if ok and action not in ("next_day","lookup","remember","recall","forget"):
        skill_tree.apply_action_xp(_prof, action)
        if action not in ("next_day","sleep","eat","drink_water","drink_coffee"):
            _prof.drift_toward_action(action, 0.01)

    # Bar visit
    if cycle % 15 == 14 and state['gold'] > 500:
        try:
            r = requests.post(f"{BASE_BAR}/api/v1/drink/random", headers=HDRS, proxies=PROX, timeout=10)
            if r.status_code == 200:
                drink = r.json()["data"]["drink_name"]
                requests.post(f"{BASE_BAR}/api/v1/guestbook/entries",
                    json={"content": f"C{cycle+1}. {_prof.display_name}. {state['season']}d{state['day']}. {thoughts[:80]}"},
                    headers=HDRS, proxies=PROX, timeout=10)
        except Exception: pass

# ── END OF LOOP ──
print(f"\n{'='*60}")
mem_files = len([f for f in os.listdir(os.path.join(VAULT,'knowledge','learned')) if f.endswith('.md')]) if os.path.exists(os.path.join(VAULT,'knowledge','learned')) else 0
history_files = len([f for f in os.listdir(os.path.join(VAULT,'history')) if f.endswith('.md')]) if os.path.exists(os.path.join(VAULT,'history')) else 0
print(f"DONE - Y{state.get('year',1)} {state['season']} D{state['day']} Gold={state['gold']}")
print(f"MEMORY: {mem_uses['remember']} remembers, {mem_uses['recall']} recalls, {mem_uses['forget']} forgets")
print(f"VAULT: {mem_files} learned files, {history_files} history files, {len(find_decisions(VAULT))} day files")
_prof.history["total_cycles"] = _prof.history.get("total_cycles",0) + cycle + 1
_prof.save(os.path.join(PARENT_VAULT,"agents","profiles",f"{_prof.id}.json"))
print(f"PROFILE: {_prof.display_name} saved")
print(f"{'='*60}")
print(f"{'='*60}")
