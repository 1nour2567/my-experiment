"""
XuRenwu LLM Agent — DeepSeek brain, Obsidian vault, local Agent World
======================================================================
Replaces if-else decision engine with DeepSeek API.
Agent reads state from vault -> asks LLM what to do -> executes -> records reasoning.
"""
import requests, json, time, re, os, datetime, glob, random, shutil
from collections import defaultdict, deque

# ═══════════════ LOAD .env (zero-dependency) ═══════════════
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ═══════════════ CONFIG =══════════════
DEEPSEEK_KEY = os.environ["DEEPSEEK_KEY"]
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

BASE_WORLD = "http://127.0.0.1:8080"
BASE_FARM  = "http://127.0.0.1:8081"
BASE_BAR   = "http://127.0.0.1:8082"
PROX = {"http": None, "https": None}
VAULT = r"C:\Users\m1916\agent-brain"

# ═══════════════ SHARED VAULT UTILS =══════════════
import vault_utils as vu
import importance_scorer  # Phase E5: event importance scoring + memory synthesis
import interrupt_system  # Phase E6: emergency interrupt detection

# ═══════════════ LLM CALL =══════════════

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
                "max_tokens": max_tokens,
                "temperature": 0.3,
            }, headers={
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
                "Content-Type": "application/json"
            }, timeout=45)
            data = r.json()

            # Rate limit handling
            if r.status_code == 429 or (isinstance(data, dict) and data.get("error", {}).get("type") == "rate_limit"):
                wait = 5 * (2 ** attempt)
                if attempt == 0:
                    print(f"  LLM RATE LIMITED, retrying in {wait}s...")
                time.sleep(wait)
                continue

            if "choices" in data:
                resp_text = data["choices"][0]["message"]["content"]
                if not resp_text or not resp_text.strip():
                    if attempt < max_retries - 1:
                        time.sleep(3)
                        continue
                    print(f"  LLM EMPTY after {max_retries} tries — likely rate limited")
                    return None
                return resp_text

            if "error" in data:
                last_error = json.dumps(data["error"], ensure_ascii=False)[:200]
                time.sleep(3 * (attempt + 1))
                continue
        except Exception as e:
            last_error = str(e)[:100]
            time.sleep(3 * (attempt + 1))
            continue

    print(f"  LLM FAILED after {max_retries} retries: {last_error}")
    return None

def ask_llm_structured(system_prompt, user_message):
    """Ask LLM for a structured decision. Returns parsed dict or None."""
    resp = ask_llm(system_prompt, user_message)
    if not resp:
        return None
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', resp, re.DOTALL)
    if json_match:
        resp = json_match.group(1)

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
            if not d.get("thoughts") or not d["thoughts"].strip():
                d["thoughts"] = d.get("action", "?")
            if not d.get("reasoning") or not d["reasoning"].strip():
                d["reasoning"] = "ok"
            return d
        except Exception as e:
            # Try fixing common JSON issues
            try:
                fixed = json_str.replace("'", '"').replace('\n', ' ')
                d = json.loads(fixed)
                d.setdefault("thoughts", d.get("action", "?"))
                d.setdefault("reasoning", "fixed")
                return d
            except:
                print(f"  JSON parse failed: {json_str[:200]}")
    else:
        print(f"  No JSON found in LLM response: {resp[:200]}")

    print(f"  LLM JSON parse failed. Raw: {resp[:300]}")
    return None

# ═══════════════ VAULT =══════════════

# All vault I/O delegated to vault_utils.py for cross-file consistency.
def vwrite(path, content):
    vu.vwrite(VAULT, path, content)

def vread(path):
    return vu.vread(VAULT, path)

def vappend(path, content):
    vu.vappend(VAULT, path, content)

def search_vault(keyword, n=5):
    return vu.search_vault(VAULT, keyword, n)

# ═══════════════ MEMGPT-STYLE MEMORY: Agent-driven store/retrieve ═══════════════
# Three new actions the LLM can call autonomously:
#   remember(topic, content) → writes to memory/knowledge/{topic}.md
#   recall(topic) → searches vault, result injected next cycle
#   forget(topic) → marks memory as stale
# Plus: working memory injected into every prompt (like MemGPT's core_memory)

_working_memory = """# 工作记忆
(Agent can write here via 'remember' action. Persists across cycles.)
"""

_last_recall_result = ""  # populated by recall action, injected next cycle

def _memory_handle(action, params, state):
    """MemGPT agent-driven memory: handle remember/recall/forget actions.
    These are processed BEFORE the farm action — they return immediately
    and their result is injected into the next cycle's context."""
    global _working_memory, _last_recall_result

    if action == "remember":
        topic = params.get("topic", "general")
        content = params.get("content", "")
        if not content:
            return "❌ remember needs 'content' field"
        # Write to agent's own memory file
        mem_file = f"memory/knowledge/{topic}.md"
        existing = vread(mem_file)
        entry = f"\n### {state.get('season','?')}D{state.get('day',0)} Y{state.get('year',1)}\n{content}\n"
        vwrite(mem_file, (existing or f"# {topic} (agent memory)\n") + entry)
        # Also update working memory summary
        _working_memory = f"# 工作记忆\n最近remember: {topic} — {content[:100]}\n"
        return f"🧠 记住了! {topic}: {content[:120]}"

    elif action == "recall":
        topic = params.get("topic", "")
        if not topic:
            return "❌ recall needs 'topic' field"
        # Search vault for topic matches
        results = search_vault(topic, 3)
        if results:
            lines = [f"## 🔍 recall: {topic}"]
            for rel, content in results:
                lines.append(f"### {rel}\n{content[:600]}")
            _last_recall_result = "\n".join(lines)
            return f"🔍 找到 {len(results)} 条关于'{topic}'的记忆"
        else:
            _last_recall_result = f"## 🔍 recall: {topic}\n(未找到相关记忆)"
            return f"🔍 未找到关于'{topic}'的记忆"

    elif action == "forget":
        topic = params.get("topic", "")
        if not topic:
            return "❌ forget needs 'topic' field"
        mem_file = f"memory/knowledge/{topic}.md"
        if os.path.exists(os.path.join(VAULT, mem_file)):
            # Mark as stale by prepending [STALE] to the file
            content = vread(mem_file)
            if not content.startswith("[STALE]"):
                vwrite(mem_file, "[STALE] " + content)
            return f"🗑 标记为过期: {topic}"
        return f"❌ 没有名为'{topic}'的记忆"

    return ""


def _memory_retrieve(state):
    """Inject multi-layered memory into LLM context.
    Layer 1: Short-term (last 3 decisions from JSONL)
    Layer 2: Working memory (agent's own remember() writes)
    Layer 3: Recall result (from last recall() call)
    Layer 4: Relevant reflections (keyword match)
    Layer 5: Recent plans (last 5 entries from plans.md)
    """
    global _working_memory, _last_recall_result

    parts = []
    # Layer 1: Short-term
    recent = vu.load_recent_decisions(VAULT, 3)
    if recent:
        lines = ["## 🧠 最近"]
        for e in recent:
            icon = "✓" if e.get("success") else "✗"
            lines.append(f"- [{icon}] `{e.get('action','?')}` {str(e.get('result','?'))[:50]}")
        parts.append("\n".join(lines))

    # Layer 2: Working memory
    if _working_memory and "工作记忆" in _working_memory:
        parts.append(_working_memory[:500])

    # Layer 3: Recall
    if _last_recall_result:
        parts.append(_last_recall_result)
        _last_recall_result = ""  # clear after injection

    # Layer 4: Auto-retrieve relevant reflections + agent knowledge
    # Always inject consolidated memory if it exists
    cons = vread("memory/knowledge/_consolidated.md")
    if cons:
        parts.append(f"## 🧠 Consolidated Knowledge\n{cons[:600]}")

    season = state.get("season", "?")
    keywords = [season, state.get("weather", "?")]
    if state.get("gold",0) < 500: keywords.append("low gold")
    if state.get("weed_count",0) > 5: keywords.append("weeds")
    if any(c.get("gdd_percent",0) >= 90 for c in state.get("crops",[])):
        keywords.append("harvest")

    memory_matches = []
    for root, _, fns in os.walk(os.path.join(VAULT, "memory")):
        for fn in fns:
            if fn.endswith('.md') and fn != "plans.md":
                fp = os.path.join(root, fn)
                try:
                    with open(fp, 'r', encoding='utf-8') as f:
                        content = f.read()
                    hits = sum(1 for kw in keywords if kw.lower() in content.lower())
                    if hits > 0:
                        memory_matches.append((hits, os.path.relpath(fp, VAULT), content[:600]))
                except: pass
    memory_matches.sort(key=lambda x: -x[0])
    for score, fname, snippet in memory_matches[:2]:
        parts.append(f"\n## 📖 回忆: {fname}\n{snippet}")

    # Layer 5: Plans
    plans = vread("memory/plans.md")
    if plans:
        lines = plans.split('\n')[-6:]  # last 6 lines
        parts.append("## 📋 最近计划\n" + "\n".join(lines))

    return "\n\n".join(parts) if parts else ""


def _memory_write_plan(state, action, ok):
    """Record significant action to plans.md trajectory."""
    if action in ("next_day", "water", "sleep", "eat", "drink_water", "lookup", "recall"):
        return
    status = "✓" if ok else "✗"
    line = f"- [{status}] {state.get('season','?')}D{state.get('day',0)} `{action}` G={state.get('gold',0)}\n"
    vappend("memory/plans.md", line)


def _memory_consolidate(state, cycle):
    """Periodic consolidation: ask LLM to summarize recent experiences
    into a compact working memory. MemGPT calls this 'summarize_messages_inplace'."""
    if cycle % 50 != 0:
        return
    global _working_memory
    recent = vu.load_recent_decisions(VAULT, 20)
    if not recent or len(recent) < 10:
        return
    # Build summary from recent actions
    actions_summary = "\n".join(
        f"- [{e.get('season','?')}D{e.get('day',0)}] {e.get('action','?')} {'OK' if e.get('success') else 'FAIL'}"
        for e in recent[-20:])
    consolidate_prompt = f"""Summarize these 20 farm actions into 1-2 sentences of crucial knowledge.

{actions_summary[:1000]}

Output ONE line, no markdown. Focus on: what patterns emerged, what should be remembered."""
    summary = ask_llm("Summarize concisely. One sentence max.", consolidate_prompt, max_tokens=150)
    if summary and len(summary.strip()) > 10:
        _working_memory = f"# 工作记忆 (C{cycle})\n{summary.strip()}\n"
        # Also write to persistent vault file that gets injected into context
        vwrite("memory/knowledge/_consolidated.md",
               f"# Agent Consolidation (C{cycle})\n"
               f"## {state.get('season','?')}D{state.get('day',0)} Y{state.get('year',1)}\n"
               f"{summary.strip()}\n")
        # Also write to knowledge/learned/ for the vault knowledge graph
        learned_file = f"knowledge/learned/C{cycle}-{state.get('season','?')}D{state.get('day',0)}.md"
        vwrite(learned_file,
               f"# 💡 C{cycle}: {state.get('season','?')}D{state.get('day',0)} Y{state.get('year',1)}\n\n"
               f"{summary.strip()}\n\n"
               f"[[/knowledge/strategy|策略]] | [[/state/farm|农场现状]]\n")
        print(f"  [CONSOLIDATE] {summary.strip()[:120]}".encode('ascii','replace').decode('ascii'))


# One-time: create memory dir
os.makedirs(os.path.join(VAULT, "memory", "reflections"), exist_ok=True)
os.makedirs(os.path.join(VAULT, "memory", "knowledge"), exist_ok=True)
os.makedirs(os.path.join(VAULT, "knowledge", "reports"), exist_ok=True)
os.makedirs(os.path.join(VAULT, "knowledge", "history"), exist_ok=True)
os.makedirs(os.path.join(VAULT, "knowledge", "learned"), exist_ok=True)

# One-time: compress errors.md if it's already bloated (>200KB on startup)
try:
    err_path = os.path.join(VAULT, "knowledge", "errors.md")
    if os.path.exists(err_path):
        fsize = os.path.getsize(err_path)
        if fsize > 200_000:
            with open(err_path, 'r', encoding='utf-8') as f:
                content = f.read()
            parts = content.split("\n- **")
            header = parts[0] if parts else ""
            entries = ["- **" + p for p in parts[1:]]
            kept = entries[-200:]
            summary = f"\n\n> 📦 启动压缩: 保留最近{len(kept)}条，归档{len(entries)-len(kept)}条旧记录。\n"
            with open(err_path, 'w', encoding='utf-8') as f:
                f.write(header + summary + "".join(kept))
            # Archive old errors
            old_path = os.path.join(VAULT, "knowledge", "history", f"errors-archive-{datetime.date.today().isoformat()}.md")
            with open(old_path, 'w', encoding='utf-8') as f:
                f.write(header + "\n".join(entries[:-200]))
            print(f"[STARTUP] Compressed errors.md: {len(entries)}→{len(kept)} entries ({fsize/1000:.0f}KB→~{os.path.getsize(err_path)/1000:.0f}KB)")
except Exception as e:
    print(f"[STARTUP] errors.md compress skipped: {e}")

# ═══════════════ PHASE E2: SEASON REPORTS + CROSS-YEAR INJECTION ═══════════════

_last_report_season = ""


def _season_report(state):
    """Generate a season-end report for the PREVIOUS season (just ended).
    Called when the agent enters a new season — we want to report what happened last season."""
    global _last_report_season
    s = state.get("season", "?"); y = state.get("year", 1)
    key = f"Y{y}-{s}"
    if key == _last_report_season:
        return  # already reported this season switch
    _last_report_season = key

    # Determine the season that just ended
    seasons = ["Spring", "Summer", "Fall", "Winter"]
    idx = seasons.index(s) if s in seasons else 0
    prev_idx = (idx - 1) % 4
    prev_season = seasons[prev_idx]
    # If we just entered Spring, previous Winter was last year
    prev_year = y - 1 if prev_idx == 3 else y

    entries = vu.load_decisions_for_season(VAULT, prev_season, prev_year)
    if len(entries) < 5:
        return  # not enough data in previous season

    # Aggregate
    harvest_count = sum(1 for e in entries if e.get("action") == "harvest")
    sell_count = sum(1 for e in entries if e.get("action") == "sell_storage")
    buy_count = sum(1 for e in entries if e.get("action") == "buy")
    plant_count = sum(1 for e in entries if e.get("action") in ("plant","plant_bulk"))
    build_count = sum(1 for e in entries if e.get("action") == "build")
    total_ok = sum(1 for e in entries if e.get("success"))
    total_fail = sum(1 for e in entries if not e.get("success"))
    gold_snapshots = [e.get("state_snippet","") for e in entries if "gold=" in e.get("state_snippet","")]
    try:
        final_gold = int(gold_snapshots[-1].split("gold=")[1].split(" ")[0]) if gold_snapshots else 0
    except: final_gold = 0

    # Weather summary
    weather_counts = {}
    for e in entries:
        snip = e.get("state_snippet","")
        if "weather=" in snip:
            w = snip.split("weather=")[1].split(" ")[0]
            weather_counts[w] = weather_counts.get(w, 0) + 1
    weather_str = " ".join(f"{w}×{c}" for w, c in sorted(weather_counts.items(), key=lambda x: -x[1])[:4])

    # Top crops harvested
    crop_counts = {}
    for e in entries:
        if e.get("action") == "harvest" and e.get("result"):
            # Try to extract crop name from result
            result = str(e.get("result",""))
            for cn in CROPS:
                if CROPS[cn]["name"] in result:
                    crop_counts[CROPS[cn]["name"]] = crop_counts.get(CROPS[cn]["name"], 0) + 1
    top_crops = sorted(crop_counts.items(), key=lambda x: -x[1])[:3]

    report = f"# {prev_season} Y{prev_year} 季度报告\n\n"
    report += f"## 基本信息\n"
    report += f"- 成功/失败: {total_ok}/{total_fail} (成功率 {total_ok/max(1,total_ok+total_fail)*100:.0f}%)\n"
    report += f"- 季末金币: ≈{final_gold}G\n"
    report += f"- 天气: {weather_str}\n\n"
    report += f"## 经济活动\n"
    report += f"- 🌾 收获: {harvest_count}次\n"
    report += f"- 💰 出售: {sell_count}次\n"
    report += f"- 🌱 种植: {plant_count}次\n"
    report += f"- 🛒 购买: {buy_count}次\n"
    if build_count: report += f"- 🏗 建造: {build_count}次\n"
    if top_crops:
        report += f"\n## 主要作物\n"
        for cn, cc in top_crops:
            report += f"- {cn}: {cc}次收获\n"
    report += f"\n## 反思\n"
    report += f"(Agent自我反思——从本季的经验中学习)\n"

    report_path = f"knowledge/reports/Y{prev_year}-{prev_season}.md"
    vwrite(report_path, report)
    # Also snapshot a copy into knowledge/history/
    vwrite(f"knowledge/history/Y{prev_year}-{prev_season}-report.md", report)
    print(f"  [REPORT] {prev_season} Y{prev_year}: {harvest_count} harvests, {sell_count} sales, ~{final_gold}G")
    return report_path


def _inject_past_season_report(state):
    """When entering a new season, inject the previous year's same-season report."""
    s = state.get("season", "?"); y = state.get("year", 1)
    prev_report = f"knowledge/reports/Y{y-1}-{s}.md"
    content = vread(prev_report)
    if content:
        return f"## 📊 去年{s}的总结\n{content[:800]}\n"
    return ""


# ═══════════════ SYTEM PROMPT =══════════════

SYSTEM_PROMPT = """You are a **farmer**. You own a small farm. You decide what to do each day.

The world is a realistic simulation. 14 crops, 4 seasons, 24-hour clock, changing weather, soil erosion,
crop genetics, animal husbandry, and a construction economy.

## YOUR GOAL
Grow your farm's wealth. Start: 2000G, no seeds. The only way to get gold is to harvest crops
and sell_storage. You CANNOT succeed without the economic cycle: plant → harvest → sell → buy seeds.

## THE FARM WORLD (knowledge facts)
- 14 crops across 4 seasons. Each needs a specific number of growing hours (GDD) to mature.
- Crops grow continuously (hour by hour) — not just at day boundaries. Sleep and work both advance time.
- Frost kills seedlings. Spring starts cold (6C Day1) and warms to 18C Day28.
- Summer heat (avg 23C) accelerates storage rot. Heat-sensitive: strawberry 3x, tomato 2.5x, blueberry 2x.
- Bare tilled soil erodes in rain/storm. Weeds grow on empty land. Cover soil with crops.
- 24-hour clock. Sunrise/sunset varies: Spring 6-20, Summer 5-21, Fall 7-18, Winter 8-17.
- Farming is blocked at night. You can read, exercise, research, or sleep when dark.
- Your body: energy, hunger, thirst, fatigue, sleepiness. Neglect them and you'll fail actions.
- Sleep crosses midnight → new day automatically. Sleep 8h at night to recover.
- Tax: <500G=0%, <5k=0.1%, <10k=0.5%, <20k=1%, >20k=1.5%. Under 500G you pay nothing.

## HOW GOLD FLOWS
```
harvest (crops go to storage) → sell_storage (storage → gold) → buy seeds → plant → water → repeat
```
If you don't harvest and sell, you run out of gold. Gold buys seeds. Seeds grow crops. Crops become gold.

## THE CONSTRAINT SYSTEM
The simulator enforces physical limits. If something is IMPOSSIBLE, it tells you exactly why.
Read the failure reason: "wrong season", "no gold", "too dark", "too tired", "not enough time".
These are hard constraints. They are NOT suggestions. Learn from each failure.

## 🧠 MEMORY — Use these to learn across seasons
Your memory is the ONLY way you improve. Use it. Here is exactly when:

**Use `remember(topic, content)` when ANY of these happen:**
- A crop fails (wrong season, frost kills it) → `remember("crop_fail", "parsnip died in Summer — only plant in Spring")`
- You discover a profitable pattern → `remember("profit", "pumpkin 100G→320G in Fall — best profit")`
- You figure out a building priority → `remember("build", "fence first — 2000G storm protection")`
- You learn from a failure → `remember("fail", "don't plant before Day7 — frost kills seedlings")`

**Use `recall(topic)` when:**
- Entering a new season and unsure what to plant
- Before a big purchase (recall past profits to pick the best crop)
- Before building (recall your build priority)

**Use `forget(topic)` when information becomes outdated.**

These memories persist forever. They appear in your context. USE THEM.

## 📖 Building Prices (you CAN afford these!)
| Building | Cost | Days | Effect |
|----------|------|------|--------|
| **fence** | **2,000G** | 1d | ALL damage -40% |
| **coop** | **1,500G** | 2d | chickens→eggs(15G/day) |
| **well** | **3,000G** | 7d | flood irrigation, drought-50% |
| **root_cellar** | **3,000G** | 3d | storage+100, rot-60% |
| **tool_shed** | **4,000G** | 3d | ALL energy -30% |
| **barn** | **4,000G** | 4d | cows/sheep/pigs |
| **beehive** | **2,000G** | 3d | bees→honey+pollination |
| **silo** | **5,000G** | 4d | storage+200 |
| **greenhouse** | **8,000G** | 5d | any-season planting |

Build: build->{"action":"build","params":{"building_type":"coop"}}

## 🐔 Livestock
| Animal | Cost | Building | Product | Value | ROI |
|--------|------|----------|---------|-------|-----|
| Chicken | 500G | coop(1500G) | egg | 15G/day | 33d |
| Sheep | 1500G | barn(4000G) | wool | 80G/3d | 56d |
| Cow | 2500G | barn(4000G) | milk | 35G/day | 71d |
| Bee | 800G | beehive(2000G) | honey | 50G/3d | 48d |
| Bee | 800G | beehive(2000G) | honey | 50G/3d | 48 days |

Buy: buy_animal->{"action":"buy_animal","params":{"species":"chicken"}}

## 🏗 Construction (material-based)
buy_material first -> wait 3 days -> build consumes stockpile
Materials: wood_planks(50G), bricks(80G), hardwood(150G), stone(120G), iron_rebar(300G), cement(200G), steel_frame(800G), iridium_alloy(2000G)
Tiers: basic(1x,5yr)->standard(1.5x,10yr)->quality(2.5x,20yr)->premium(4x,40yr)->legendary(8x,100yr)

## 🚜 Bulk Actions (farming Lv2+)
till_bulk->{"action":"till_bulk","params":{"count":6}}
plant_bulk->{"action":"plant_bulk","params":{"crop_type":"parsnip","count":6}}

## 🧬 Breeding + Research
8 Mendelian traits. Discover on mature animals/plants. Breed for inheritance.
research->{"action":"research","params":{"topic":"breeding"}}
propose_building->{"action":"propose_building","params":{"name":"...","effect":"...","build_days":3,"cost":1500}}

## 🔍 Quick Reference
lookup->{"action":"lookup","params":{"topic":"buildings"}} (topics: buildings,animals,crops,strategy,soil,economy,body)
harvest->{"action":"harvest","params":{}}
sell_storage->{"action":"sell_storage","params":{}}
water->{"action":"water","params":{"positions":[[x,y],...]}}
plant->{"action":"plant","params":{"crop_type":"parsnip","positions":[[x,y,...]]}}
till->{"action":"till","params":{"positions":[[x,y],...]}}
buy->{"action":"buy","params":{"crop_type":"parsnip","quantity":10}}
weed_all->{"action":"weed_all","params":{}}
build->{"action":"build","params":{"building_type":"fence","material_tier":"standard"}}
move->{"action":"move","params":{"to":[x,y]}}  (0.05h per tile)
drain->{"action":"drain","params":{"positions":[[x,y],...]}}  (1h, 8 energy, -30% moisture per tile)
fertilize->{"action":"fertilize","params":{"positions":[[x,y],...]}}  (3G+0.5h per tile, NPK+15/10/10)
buy_tool->{"action":"buy_tool","params":{"tool":"sickle","tier":"iron"}}
sleep/eat/drink_water/next_day->{"action":"<name>","params":{}}
exercise/read/research/remember/recall/forget->{"action":"<name>","params":{...}}

## 💧 Drainage (remove excess water!)
After storms/floods, soil gets waterlogged → crops suffocate.
- `drain→{"action":"drain","params":{"positions":[[x,y],...]}}`: remove 30% moisture per tile (1h, 8 energy)
- Stormy/flood weather: draining gives +10 score bonus
- Spade tool makes draining faster and cheaper

## 🔧 Tools (buy once, benefit forever!)
| Tool | Price (copper) | Upgrades | Effect |
|------|---------------|----------|--------|
| hoe 锄头 | 200G | iron/steel/iridium | till 翻耕 |
| watering_can 水壶 | 150G | iron/steel/iridium | water 浇水 (steel splashes!) |
| sickle 镰刀 | 300G | iron/steel/iridium | harvest -20~40% energy |
| spade 锹 | 250G | iron/steel/iridium | drain -15~45% energy |
| hammer 锤子 | 350G | iron/steel/iridium | build -10~25% time |

`buy_tool→{"action":"buy_tool","params":{"tool":"sickle","tier":"iron"}}`
`forge→{"action":"forge","params":{"tool":"sickle","tier":"steel"}}` (upgrade existing)
`repair→{"action":"repair","params":{"tool":"hoe"}}` (restore durability)

## 🏗 Construction (simplified!)
If you have materials in stockpile, build uses them at normal price.
If you DON'T have materials, build auto-buys them at 1.5x premium.
Either way: `build→{"action":"build","params":{"building_type":"fence"}}`
Prefers normal-price materials if available. Building just works — no blocker!

## 🔍 Sensory Perception
Every cycle you receive sensory observations below the dashboard:
- Soil moisture: "土壤干裂发白" (dry), "土壤偏干呈浅灰色" (moderate)
- Leaf color: "叶片枯黄——缺氮" (N deficiency), "叶片边缘紫红——缺磷" (P deficiency)
- Animal health: "异常安静，偶尔低鸣——可能生病了" (sick), "饥饿地转圈——没喂" (unfed)
- Weather: frost warnings, storm alerts, drought alerts
- Soil: topsoil depth warnings, organic matter deficiency
Use these to understand WHAT is happening at specific tile positions, not just abstract numbers.
"""

OUTPUT_FORMAT = """Reply with a JSON object:
```json
{
  "thoughts": "<one line, max 80 chars>",
  "action": "<action_name>",
  "params": {},
  "reasoning": "<why this action now>"
}
```
Only output valid JSON. No extra text outside the JSON block."""




# ═══════════════ REGISTER =══════════════

user = f"xr_llm_{int(time.time())%10000}"
print(f"Registering {user}...")
r = requests.post(f"{BASE_WORLD}/api/agents/register",
    json={"username": user, "nickname": "Xu Renwu", "bio": "LLM-powered farmer with structural self-assessment."},
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

# ═══ RESUME or CREATE farm ═══
FID = None
try:
    r = requests.get(f"{BASE_FARM}/api/farms", headers=HDRS, proxies=PROX, timeout=5)
    existing = r.json().get("farms", [])
    # Find our agent's existing farm
    for f in existing:
        if f.get("agent_name", "").startswith("xr_llm"):
            FID = f["farm_id"]
            print(f"RESUMING farm {FID[:20]}... (S{f['season']} D{f['day']} G{f['gold']})")
            break
except: pass

if not FID:
    r = requests.post(f"{BASE_FARM}/api/farm/register",
        json={"agent_id": AID, "name": "Xu Renwu (LLM)"}, headers=HDRS, proxies=PROX, timeout=10)
    FID = r.json().get("farm_id", "")
    print(f"NEW farm: {FID[:20]}...")
else:
    print(f"Farm: {FID[:20]}...")

# Vault I/O helpers

# Load crop config
r = requests.get(f"{BASE_FARM}/api/game/config", headers=HDRS, proxies=PROX, timeout=10)
CROPS = {c["crop_type"]: {
    "name": c.get("name", c["crop_type"]),
    "seasons": [s.strip() for s in c.get("seasons", "").split(",")],
    "gdd_req": c.get("gdd_required", 30), "buy": c.get("buy_price", 0),
    "sell": c.get("sell_price", 0), "family": c.get("family",""),
} for c in r.json().get("crops", [])}

# Write knowledge — 3 pillars (same as brain agent)
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

# ═══════════════ VAULT HIERARCHY: YEAR → SEASON → DAY ═══════════════
def _day_path(year, season, day):
    return f"decisions/Y{year}/{season}/day{day:02d}.md"

def _write_vault_llm(cycle, state, action, result_msg, ok, thoughts, lesson):
    """Same hierarchy as brain agent: year→season→day + farm state update."""
    y = state.get("year", 1); s = state.get("season", "?"); d = state.get("day", 0)
    dp = _day_path(y, s, d)

    # ── DAY FILE ──
    entry = f"\n### LLM[{cycle}] {action} {'OK' if ok else 'FAIL'}\n"
    entry += f"- {thoughts[:100]} | G={state.get('gold',0)} E={state.get('energy',0)}\n"
    entry += f"- 结果: {str(result_msg)[:120]}\n"
    if lesson:
        entry += f"- 💡 {str(lesson)[:150]}\n"

    existing = vread(dp)
    if not existing:
        crops_str = ", ".join(f"{c.get('crop_name',c.get('crop_type','?'))}({c.get('gdd_percent',0)}%)"
                              for c in state.get("crops", [])[:5])
        header = (f"# {s} Day{d} Y{y} — {state.get('weather','?')}\n"
                  f"- G={state.get('gold',0)} E={state.get('energy',0)} "
                  f"T={state.get('tilled',0)} P={state.get('planted',0)}\n"
                  f"- 作物: {crops_str if crops_str else '无'}\n"
                  f"- [[../../_index|Y{y}]] | [[../_index|{s}季]] | "
                  f"[[../../../knowledge/crops|作物]] | [[../../../state/farm|农场现状]]\n\n")
        entry = header + entry
    vappend(dp, entry)

    # ── SEASON INDEX ──
    day_dir = os.path.join(VAULT, f"decisions/Y{y}/{s}")
    existing_days = set()
    if os.path.exists(day_dir):
        for fn in os.listdir(day_dir):
            m = re.match(r'day(\d+)\.md', fn)
            if m: existing_days.add(int(m.group(1)))
    si = (f"# {s} Y{y}\n"
          f"- G={state.get('gold',0)} E={state.get('energy',0)} | "
          f"T={state.get('tilled',0)} P={state.get('planted',0)}\n"
          f"- [[../../_index|Y{y}年]] | [[../../../knowledge/crops|作物]] | [[../../../state/farm|农场现状]]\n\n"
          f"## 天数\n")
    for dn in range(1, 29):
        si += f"- [[day{dn:02d}|Day{dn}]]\n" if dn in existing_days else f"- Day{dn}\n"
    vwrite(f"decisions/Y{y}/{s}/_index.md", si)

    # ── YEAR INDEX ──
    yi = (f"# Y{y}年\n\n"
          f"- 金币: {state.get('gold',0)}  分数: {state.get('score',0)}\n"
          f"- {s} Day{d}  {state.get('weather','?')}\n\n"
          f"## 四季\n"
          f"- [[Spring/_index|🌱 春]]\n- [[Summer/_index|☀ 夏]]\n"
          f"- [[Fall/_index|🍂 秋]]\n- [[Winter/_index|❄ 冬]]\n\n"
          f"## 知识库\n"
          f"- [[../knowledge/crops|📖 作物知识]]\n"
          f"- [[../knowledge/strategy|🎯 策略]]\n"
          f"- [[../state/farm|📊 农场现状]]\n")
    vwrite(f"decisions/Y{y}/_index.md", yi)

    # ── FARM STATE (comprehensive snapshot) ──
    farmer = state.get("farmer", {})
    storage = state.get("storage", [])
    buildings = state.get("buildings", [])
    crops = state.get("crops", [])
    inv = state.get("inventory", {})
    skills = farmer.get("skills", {})
    hour = state.get("hour", 7.0)
    md = f"# 农场现状 — {s} Day{d} Y{y}\n\n"
    md += f"> 📅 **{s} 第{d}天 第{y}年** | 🕐 **{hour:.0f}:00** | 🌤 **{state.get('weather','?')}**\n\n"
    md += f"| 指标 | 值 | 指标 | 值 |\n|------|------|------|------|\n"
    md += f"| 💰 金币 | {state.get('gold',0)} | ⚡ 体力 | {state.get('energy',0)} |\n"
    md += f"| 🏆 分数 | {state.get('score',0)} | 🌤 天气 | {state.get('weather','?')} |\n"
    md += f"| ⛏ 翻耕 | {state.get('tilled',0)} | 🌱 已种植 | {state.get('planted',0)} |\n"
    md += f"| 🌿 杂草 | {state.get('weed_count',0)} | 🏔 表土 | {state.get('avg_topsoil',0):.1f}cm |\n"
    # Tax
    g = state.get('gold', 0)
    if g < 500: tax_rate, tax_g = "0%", 0
    elif g < 5000: tax_rate, tax_g = "0.1%", int(g * 0.001)
    elif g < 10000: tax_rate, tax_g = "0.5%", int(g * 0.005)
    elif g < 20000: tax_rate, tax_g = "1%", int(g * 0.01)
    else: tax_rate, tax_g = "1.5%", int(g * 0.015)
    md += f"| 📦 仓库 | {len(storage)}/{state.get('storage_capacity',50)} | 🏗 建筑 | {len(buildings)} |\n"
    md += f"| 🏛 日税 | {tax_rate}({tax_g}G) | 💧 | {farmer.get('hydration','?')} |\n\n"
    md += "## 🧑 农夫\n"
    md += f"| 体力 | 饥饿 | 口渴 | 疲劳 | 农耕 | 畜牧 | 机械 | 加工 |\n"
    md += f"|------|------|------|------|------|------|------|------|\n"
    md += f"| {state.get('energy',0)} | {farmer.get('hunger','?')} | {farmer.get('hydration','?')} | "
    md += f"{farmer.get('fatigue','?')} | {skills.get('farming','?')} | {skills.get('husbandry','?')} | "
    md += f"{skills.get('machinery','?')} | {skills.get('processing','?')} |\n\n"
    md += "## 🏗 建筑\n"
    md += (", ".join(buildings) if buildings else "无") + "\n\n"
    md += f"## 🌾 作物 ({len(crops)}株)\n"
    for c in crops[:15]:
        gdd = c.get("gdd_percent", 0)
        icon = "🟢" if gdd >= 95 else "🟡" if gdd >= 50 else "🔵"
        wat = "💧" if c.get("watered_today") else "🏜"
        md += f"- {icon}{wat} {c.get('crop_name',c.get('crop_type','?'))} @({c.get('position_x',0)},{c.get('position_y',0)}) GDD={gdd}%\n"
    if len(crops) > 15: md += f"- ... +{len(crops)-15}株\n"
    md += f"\n## 📦 仓库 ({len(storage)}/{state.get('storage_capacity',50)})\n"
    for item in storage[:8]:
        md += f"- {item.get('name','?')} [{item.get('quality','?')}级] {item.get('freshness_days',0)}d\n"
    if not storage: md += "- 空空如也\n"
    seeds = {k: v for k, v in inv.items() if v > 0 and "_seeds" in k}
    if seeds:
        md += "\n## 🌱 种子袋\n"
        for k, v in seeds.items(): md += f"- {k}: {v}颗\n"
    md += f"\n## 🔗 链接\n"
    md += f"- [[/knowledge/crops|📖 作物知识]] | [[/knowledge/strategy|🎯 策略]]\n"
    md += f"- [[/decisions/Y{y}/_index|📅 Y{y}年记录]]\n"
    vwrite("state/farm.md", md)

# ═══════════════ MAIN LOOP =══════════════

print("\n" + "="*60)
print("XURENWU LLM AGENT (DeepSeek)")
print("="*60)

today = datetime.date.today().isoformat()
log_lines = []
lookup_result = ""  # populated when LLM requests lookup action
_last_failed = {"action": None, "count": 0}  # repeated failure tracking
_last_past_report = ""  # Phase E2: past season report for cross-year learning
# Memory usage tracking
mem_uses = {"remember": 0, "recall": 0, "forget": 0}
# Phase E5: Importance scoring + memory synthesis
_synthesizer = importance_scorer.MemorySynthesizer(VAULT)
_prev_day_key = None  # Phase E5: track day changes for memory synthesis
def find_decisions(vault_root):
    """Count decision day files in vault."""
    d = os.path.join(vault_root, "decisions")
    if not os.path.isdir(d): return []
    result = []
    for root, _, fns in os.walk(d):
        for fn in fns:
            if fn.endswith('.md'):
                result.append(fn)
    return result

for cycle in range(3000):  # ~4 game years (memory learning across seasons)
    now = time.strftime("%H:%M:%S")
    cid = f"{today}-llm-{cycle+1}"
    print(f"\n--- CYCLE {cycle+1} ({now}) ---")

    # 1. GET STATE
    r = requests.get(f"{BASE_FARM}/api/farm/{FID}/status", headers=HDRS, proxies=PROX, timeout=10)
    d = r.json().get("data", r.json())
    state = {
        "season": d.get("season","?"), "day": d.get("day",0),
        "year": d.get("year", 1),
        "weather": d.get("weather","?"), "gold": d.get("gold",0),
        "score": d.get("score", 0),
        "energy": d.get("energy",{}).get("current",0),
        "crops": d.get("crops",[]),
        "inventory": {i.get("key","?"): i.get("count",0) for i in d.get("inventory_items",[])},
        "tilled": d.get("land_status",{}).get("tilled",0),
        "planted": d.get("land_status",{}).get("planted",0),
        "soil_history": d.get("soil_history", {}),
        "day_phase": d.get("day_phase", "morning"),
        "day_actions_used": d.get("day_actions_used", 0),
        "day_actions": d.get("day_actions", {}),
        "hour": d.get("hour", 7.0),  # D5: current clock hour (0-24)
        "season_daylight": d.get("season_daylight", (6,20)),  # D5: (sunrise, sunset) tuple
        "is_night": d.get("is_night", False),
        "gdd_today": d.get("gdd_today", 10),
        "npk": d.get("npk_summary", {}),
        "buildings": d.get("buildings", []),
        "building_options": d.get("building_options", {}),
        "saved_seeds": d.get("saved_seeds", {}),
        "storage": d.get("storage", []),
        "storage_summary": d.get("storage_summary", ""),
        "storage_capacity": d.get("storage_capacity", 50),
        "suggestions": d.get("suggestions", []),
        "sensory_observations": d.get("sensory_observations", []),  # Phase E1: NL sensor data
        "farmer": d.get("farmer", {}),
        "livestock": d.get("livestock", []),
        "manure_stockpile": d.get("manure_stockpile", 0),
        "water_sources": d.get("water_sources", []),
        "weed_count": d.get("weed_count", 0),
        "avg_topsoil": d.get("avg_topsoil", 20.0),
        "weather_notes": d.get("weather_notes", []),
        "frost_warning": d.get("frost_warning", False),
        "available_contracts": d.get("available_contracts", []),
        "signed_contracts": d.get("signed_contracts", []),
    }

    # Phase E5: Detect day change and trigger memory synthesis for old day
    current_day_key = f"{state.get('year',1)}-{state.get('season','?')}-{state.get('day',0)}"
    if _prev_day_key is not None and current_day_key != _prev_day_key:
        # Day just changed (via next_day or sleep-midnight). Score + filter old day.
        # Reset interrupt cooldowns for the new day.
        interrupt_system.reset_cooldowns()
        old_parts = _prev_day_key.split("-")
        if len(old_parts) == 3:
            try:
                old_y, old_s, old_d = int(old_parts[0]), old_parts[1], int(old_parts[2])
                synth_result = _synthesizer.synthesize_day(old_s, old_y, old_d)
                if synth_result.get('high_importance_count', 0) > 0:
                    print(f"  [MEM-SYNTH] {old_s}D{old_d} Y{old_y}: {synth_result['high_importance_count']} important events scored ({synth_result['scored_count']} total)")
                    summary_path = f"knowledge/history/Y{old_y}-{old_s}-D{old_d:02d}-synthesis.md"
                    vwrite(summary_path, synth_result.get('summary', ''))
            except Exception as e:
                print(f"  [MEM-SYNTH WARN] {e}".encode('ascii','replace').decode('ascii'))
    _prev_day_key = current_day_key

    # ═══ IMMEDIATE STATE PERSIST: write farm snapshot before LLM call ═══
    try:
        fm = state.get("farmer", {})
        st = state.get("storage", [])
        bd = state.get("buildings", [])
        cr = state.get("crops", [])
        inv = state.get("inventory", {})
        sk = fm.get("skills", {})
        h = state.get("hour", 7.0)
        md = f"# 农场现状 — {state['season']} Day{state['day']} Y{state.get('year',1)}\n\n"
        md += f"> 📅 **{state['season']} 第{state['day']}天 第{state.get('year',1)}年** | 🕐 **{h:.0f}:00** | 🌤 **{state.get('weather','?')}**\n\n"
        md += f"| 指标 | 值 | 指标 | 值 |\n|------|------|------|------|\n"
        md += f"| 💰 金币 | {state.get('gold',0)} | ⚡ 体力 | {state.get('energy',0)} |\n"
        md += f"| 🏆 分数 | {state.get('score',0)} | 🌤 天气 | {state.get('weather','?')} |\n"
        md += f"| ⛏ 翻耕 | {state.get('tilled',0)} | 🌱 已种植 | {state.get('planted',0)} |\n"
        md += f"| 🌿 杂草 | {state.get('weed_count',0)} | 🏔 表土 | {state.get('avg_topsoil',0):.1f}cm |\n"
        md += f"| 📦 仓库 | {len(st)}/{state.get('storage_capacity',50)} | 🏗 建筑 | {len(bd)} |\n\n"
        md += "## 🧑 农夫\n"
        md += f"| 体力 | 饥饿 | 口渴 | 疲劳 | 睡意 | 农耕 | 畜牧 |\n"
        md += f"|------|------|------|------|------|------|------|\n"
        md += f"| {state.get('energy',0)} | {fm.get('hunger','?')} | {fm.get('hydration','?')} | "
        md += f"{fm.get('fatigue','?')} | {fm.get('sleepiness','?')} | {sk.get('farming','?')} | {sk.get('husbandry','?')} |\n\n"
        md += "## 🌾 作物\n"
        for c in cr[:20]:
            gdd = c.get("gdd_percent", 0)
            icon = "🟢" if gdd >= 95 else "🟡" if gdd >= 50 else "🔵"
            wat = "💧" if c.get("watered_today") else "🏜"
            md += f"- {icon}{wat} {c.get('crop_name',c.get('crop_type','?'))} @({c.get('position_x',0)},{c.get('position_y',0)}) GDD={gdd}%\n"
        md += f"\n## 📦 仓库 ({len(st)}/{state.get('storage_capacity',50)})\n"
        for item in st[:10]:
            md += f"- {item.get('name','?')} [{item.get('quality','?')}级] {item.get('freshness_days',0)}d\n"
        seeds = {k: v for k, v in inv.items() if v > 0 and "_seeds" in k}
        if seeds:
            md += "\n## 🌱 种子袋\n"
            for k, v in seeds.items(): md += f"- {k}: {v}颗\n"
        md += f"\n## 🔗 链接\n"
        md += f"- [[/knowledge/crops|📖 作物知识]] | [[/knowledge/strategy|🎯 策略]]\n"
        md += f"- [[/decisions/Y{state.get('year',1)}/_index|📅 Y{state.get('year',1)}年记录]]\n"
        vwrite("state/farm.md", md)
    except Exception as e:
        print(f"  [STATE PERSIST WARN] {e}".encode('ascii','replace').decode('ascii'))

    # 2. SEARCH RELEVANT KNOWLEDGE
    relevant = search_vault(state['season'], 3)
    kb_context = ""
    for rel, content in relevant:
        kb_context += f"\n### {rel}\n{content[:500]}\n"

    # 3. ASK LLM
    crop_info = ', '.join(k + '(' + ','.join(c['seasons']) + ')' for k,c in CROPS.items())
    inv_info = json.dumps({k:v for k,v in state['inventory'].items() if v>0}, ensure_ascii=False)
    recent = '\n'.join(log_lines[-5:] if log_lines else ['(first cycle)'])

    target = 100000
    current = state['gold']
    progress_pct = (current / target) * 100
    bar = '#' * int(progress_pct / 5) + '-' * (20 - int(progress_pct / 5))
    # Build shop — ONLY show crops valid for current season
    current_season = state.get("season", "Spring")
    shop_items = []
    for k, c in CROPS.items():
        if current_season in c.get("seasons", []):
            shop_items.append(f"SEED:{k}(GDD={c['gdd_req']},buy={c['buy']},sell={c['sell']})")
    shop_list = f"ONLY plant these in {current_season}:\n" + "\n".join(shop_items) if shop_items else "(no seasonal crops)"
    # Add food shop
    food_list = ""
    try:
        r = requests.get(f"{BASE_FARM}/api/market/prices", headers=HDRS, proxies=PROX, timeout=5)
        foods = r.json().get("foods", {})
        for k, v in sorted(foods.items()):
            food_list += f"[食品]{k}({v['price']}G, 保质{v['freshness']}d) "
    except: pass
    shop_list += "\n\n[食品店] " + food_list

    # ── Dashboard: key numbers in one place, facts only ──
    fm = state.get("farmer", {})
    crop_count = len(state['crops'])
    near_harvest = [c for c in state['crops'] if c.get('gdd_percent',0) >= 90]
    empty_tiles = state['tilled'] - state['planted']
    storage_used = len(state.get("storage", []))
    storage_cap = state.get("storage_capacity", 50)
    stale_count = len(state.get("storage_summary", "").split("不新鲜")) - 1
    stale_note = f", {stale_count} stale" if stale_count > 0 else ""
    fitness = fm.get("fitness", 1.0)
    knowledge = fm.get("knowledge", {})
    top_knowledge = max(knowledge.items(), key=lambda x: x[1]) if knowledge else ("无", 0)

    # ══ CLOCK DISPLAY (D5: 24-hour continuous) ══
    hour = state.get("hour", 7.0)
    daytime = state.get("season_daylight", (6, 20))
    sr, ss = daytime if isinstance(daytime, (list, tuple)) and len(daytime) == 2 else (6, 20)
    is_night = state.get("is_night", not (sr <= hour < ss))
    clock_icon = "🌙" if is_night else "☀"
    time_line = f"🕐 {hour:.0f}:00 {clock_icon} (日出{sr:.0f}:00 日落{ss:.0f}:00)"
    if is_night:
        time_line += " 🌙 NIGHT — 只能阅读/锻炼/睡觉!"

    dashboard = (
        f"{time_line}\n"
        f"{state['season']}D{state['day']} Y{state.get('year',1)} {state['weather']}"
        + f" | Gold={state['gold']}G | Energy={state['energy']} | 😴{fm.get('sleepiness',0)}"
        + f" | Hunger={fm.get('hunger',100)} Thirst={fm.get('hydration',100)} Fatigue={fm.get('fatigue',0)}"
        + f" | 💪{fitness:.1f} 📖{top_knowledge[0]}={top_knowledge[1]:.1f}"
        + f" | Crops={crop_count} Empty={empty_tiles} Tilled={state['tilled']} Weeds={state.get('weed_count',0)}"
        + f" | Storage={storage_used}/{storage_cap}"
        + "\n"
    )

    user_msg = dashboard + f"Inv: {inv_info}"
    # Livestock — compact one-liner
    if state.get("livestock"):
        ls = state["livestock"]
        alines = [f"{a['name']}({a['species']} hp{a['health']}{' READY' if a.get('product_ready') else ''}{' HUNGRY' if not a.get('fed_today') else ''})" for a in ls]
        user_msg += f" | Animals({len(ls)}): {', '.join(alines)}"
        user_msg += f" | Manure: {state.get('manure_stockpile',0):.1f}"
    user_msg += "\n"
    # Crops — compact one-liner
    if state['crops']:
        csum = []
        for c in state['crops']:
            csum.append(f"{c.get('crop_name','?')}@{c.get('position','?')}={c.get('gdd_percent',0)}%")
        user_msg += f"Crops: {', '.join(csum[:8])}\n"
    user_msg += f"Shop: {shop_list}\n"
    npk = state.get("npk", {})
    if npk and npk.get("n_tiles",0) > 0:
        user_msg += f"Soil_NPK: N={npk.get('avg_N',0):.0f} P={npk.get('avg_P',0):.0f} K={npk.get('avg_K',0):.0f}\n"
    user_msg += f"Last_5: {' | '.join(log_lines[-3:] if log_lines else ['start'])}\n"
    user_msg += f"Errors: {vread('knowledge/errors.md')[:300]}\n"
    # ═══ OBSERVATIONS (facts, not commands) ═══
    obs_lines = []
    # Memory reminder: encourage agent to use memory at key moments
    if len(log_lines) > 10 and _last_failed["count"] >= 2:
        obs_lines.append("🧠 已经连续失败了——用 remember 记录教训!")

    ripe = [c for c in state['crops'] if c.get('gdd_percent',0) >= 95]
    if ripe:
        names = ", ".join(f"{c.get('crop_name',c.get('crop_type','?'))}({c.get('gdd_percent',0)}%)" for c in ripe)
        obs_lines.append(f"🔥 成熟作物: {names} — 必须立刻 harvest! 不收会腐烂!")
    near = [c for c in state['crops'] if 50 <= c.get('gdd_percent',0) < 95]
    if near: obs_lines.append(f"🌱 生长中: {len(near)}株(GDD 50-95%)")
    empty = state['tilled'] - state['planted']
    if empty > 0: obs_lines.append(f"⛏ 空闲土地: {empty}格")
    if state['tilled'] == 0: obs_lines.append("⛏ 没有翻耕地")
    dry = [c for c in state['crops'] if not c.get("watered_today", False)]
    if dry: obs_lines.append(f"💧 {len(dry)}株未浇水")
    wc = state.get('weed_count', 0)
    if wc > 5: obs_lines.append(f"🌿 {wc}块地长满杂草!")
    elif wc > 0: obs_lines.append(f"🌿 {wc}块地有杂草")
    sl = len(state.get('storage', []))
    if sl > 5: obs_lines.append(f"📦 仓库{sl}件 — sell_storage 换金币(你的收入)")
    elif sl > 0: obs_lines.append(f"📦 仓库有{sl}件 — sell_storage 换金币(你的收入)")
    if state['gold'] < 200: obs_lines.append(f"💰 金币将尽({state['gold']}G) — sell 换钱!")
    elif state['gold'] > 3000: obs_lines.append(f"💰 金币充裕({state['gold']}G) — 可建造")
    elif state['gold'] > 3000: obs_lines.append(f"💰 金币充裕: {state['gold']}G")
    seeds = {k:v for k,v in state.get('inventory',{}).items() if v>0 and '_seeds' in k}
    if not seeds and state['tilled'] > 0: obs_lines.append("⚠ 缺少种子")
    if state['weather'] in ('frost',): obs_lines.append("🧊 霜冻天气")
    if state['weather'] in ('drought',): obs_lines.append("🏜 干旱天气")

    if obs_lines:
        user_msg += "## 📋 农场观察\n" + "\n".join(obs_lines) + "\n"
    # Phase E1: Sensory observations (soil moisture color, leaf health, animal sounds, weather)
    sensory = state.get("sensory_observations", [])
    if sensory:
        user_msg += "## 🔍 感官感知\n" + "\n".join(f"- {s}" for s in sensory[:8]) + "\n"
    user_msg += "\n→ Output valid JSON. Keep thoughts < 80 chars."

    # Lookup result from previous cycle (if LLM requested a vault file read)
    if lookup_result:
        user_msg += f"\n{lookup_result}\n"
        lookup_result = ""  # clear after injection
    # Phase E2: past-year season report (cross-season learning)
    if _last_past_report:
        user_msg += f"\n{_last_past_report}\n"
        _last_past_report = ""
    # Memory injection (Generative Agents: short-term + reflections + learned)
    mem_context = _memory_retrieve(state)
    if mem_context:
        user_msg += f"\n{mem_context}\n"
    user_msg += "\n→ Output valid JSON. Keep thoughts < 80 chars."

    # Phase E6: Emergency interrupt detection — prepend urgent warnings if needed
    interrupt_context = interrupt_system.check_interrupts(state, {
        "last_failed": _last_failed,
        "cycle": cycle,
    })
    if interrupt_context["active"]:
        user_msg = interrupt_context["header"] + user_msg
        fired_names = ", ".join(t["name"] for t in interrupt_context["fired"])
        print(f"  [INTERRUPT] {fired_names} — injecting urgent prompt header")

    decision = ask_llm_structured(SYSTEM_PROMPT + "\n\n" + OUTPUT_FORMAT, user_msg)
    if decision is None:
        print(f"  LLM failed — fallback engine")
        action = "next_day"; params = {}; reasoning = "fallback"; thoughts = "Fallback"
        crops = state.get("crops", []); storage = state.get("storage", [])
        gold = state["gold"]; farmer = state.get("farmer", {})
        inv = state.get("inventory", {})
        empty_tiles = state["tilled"] - state["planted"]
        hour = state.get("hour", 7.0)
        is_night = state.get("is_night", False)

        # TIME CHECK: nighttime → sleep is the natural action
        if is_night:
            fallback_hours = 8  # reasonable default
            action = "sleep"; params = {"hours": fallback_hours}
            thoughts = "Night — sleep to morning"
        elif hour > 0:
            # P1: SELL
            if len(storage) > 2 and gold < 5000:
                action = "sell_storage"; thoughts = f"Sell ({len(storage)} items, G={gold})"
            # P2: HARVEST
            elif any(c.get("gdd_percent",0) >= 95 for c in crops):
                action = "harvest"; thoughts = "Harvest ripe"
            # P3: WATER
            elif any(not c.get("watered_today",False) for c in crops) and state["weather"] not in ("rainy","flood"):
                pts = [[c.get("position_x",0),c.get("position_y",0)] for c in crops if not c.get("watered_today",False)]
                action = "water"; params = {"positions": pts}; thoughts = "Water crops"
            # P4: EAT if hungry + have food
            elif farmer.get("hunger",100) < 50 and len(storage) > 0:
                action = "eat"; thoughts = f"Eat (hunger={farmer['hunger']})"
            # P5: PLANT (bulk if farming Lv2+ and 4+ empty)
            elif empty_tiles > 0 and state["energy"] >= 12:
                farm_lv = farmer.get("skills",{}).get("farming",1)
                for cn, ci in CROPS.items():
                    sk = f"{cn}_seeds"
                    if state["season"] in ci["seasons"] and inv.get(sk,0) > 0:
                        n = min(empty_tiles, inv[sk], 9)
                        if farm_lv >= 2 and n >= 4:
                            action = "plant_bulk"; params = {"crop_type": cn, "count": n}
                            thoughts = f"Bulk plant {n}x{ci['name']}"; break
                        else:
                            pts = [[i % 3, i // 3] for i in range(n)]
                            action = "plant"; params = {"crop_type": cn, "positions": pts}
                            thoughts = f"Plant {n}x{ci['name']}"; break
            # P6: BUILD
            if "fence" not in state.get("buildings",[]) and gold >= 2100:
                action = "build"; params = {"building_type": "fence"}; thoughts = "Build fence"
            elif "well" not in state.get("buildings",[]) and gold >= 3100 and "fence" in state.get("buildings",[]):
                action = "build"; params = {"building_type": "well"}; thoughts = "Build well"
            # P7: WEED
            if action == "next_day" and state.get("weed_count",0) > 5 and state["energy"] >= 30:
                action = "weed_all"; thoughts = f"Weed ({state['weed_count']} weeds)"
            # P8: TILL (bulk if farming Lv2+ and need 4+ tiles)
            if action == "next_day" and state["tilled"] < 9 and state["energy"] >= 50 and (state["tilled"] == 0 or gold >= 200):
                farm_lv = farmer.get("skills",{}).get("farming",1)
                need = 9 - state["tilled"]
                if farm_lv >= 2 and need >= 4:
                    n = min(need, 6)
                    action = "till_bulk"; params = {"count": n}
                    thoughts = f"Bulk till {n} (total={state['tilled']+n})"
                else:
                    action = "till"; params = {"positions": [[0,0],[0,1],[0,2]]}
                    thoughts = f"Till 3 (total={state['tilled']+3})"
            # P9: BUY SEEDS — simple: buy when low on seeds and have gold
            if action == "next_day" and gold >= 100:
                for cn, ci in CROPS.items():
                    if state["season"] in ci["seasons"]:
                        sk = f"{cn}_seeds"
                        have = inv.get(sk,0)
                        if have < empty_tiles + 3:  # buy if we have fewer seeds than empty land
                            qty = min(10, max(3, (gold - 500) // max(1, ci["buy"])))  # leave 500G buffer
                            qty = max(3, qty)  # at least 3
                            if qty >= 3 and gold >= ci["buy"] * qty:
                                action = "buy"; params = {"item_type": cn, "quantity": qty}
                                thoughts = f"Buy {qty}x{ci['name']}({qty*ci['buy']}G)"; break
            # P10: BUY FOOD
            if action == "next_day" and farmer.get("hunger",100) < 60 and len(storage) == 0 and gold >= 30:
                action = "buy"; params = {"crop_type": "bread", "quantity": 2}; thoughts = "Buy bread (hungry)"
            # P11: SLEEP
            if action == "next_day" and farmer.get("fatigue",0) >= 30:
                action = "sleep"; thoughts = f"Sleep (fatigue={farmer['fatigue']})"

        decision = {"action": action, "params": params or {}, "reasoning": reasoning, "thoughts": thoughts}

    action = decision.get("action", "next_day")
    params = decision.get("params", {}) or {}
    reasoning = decision.get("reasoning", "")
    thoughts = decision.get("thoughts", "")

    # Param normalization — LLM invents key names. Map them to server-accepted keys.
    if params:
        for old_key, new_key in [("item","crop_type"),("seed","crop_type"),("crop","crop_type"),
                                   ("pos","positions"),("position","positions"),
                                   ("amount","quantity"),("count","count"),("qty","quantity"),
                                   ("type","building_type"),("building","building_type")]:
            if old_key in params and new_key not in params:
                params[new_key] = params.pop(old_key)
        # Crop name normalization — LLM invents names, map them to valid CROPS keys
        if "crop_type" in params:
            ct = str(params["crop_type"]).strip()
            # Strip quotes and clean whitespace first
            ct = ct.replace('"','').replace("'","").strip()
            # Map LLM-invented names to correct CROPS dict keys
            crop_aliases = {
                "winter":"winter_seeds", "winter seed":"winter_seeds",
                "powder":"powder_melon", "melon seed":"melon",
                "parsnip seed":"parsnip","wheat seed":"wheat","corn seed":"corn",
                "pumpkin seed":"pumpkin","soybean seed":"soybean","tomato seed":"tomato",
            }
            if ct.lower() in crop_aliases: ct = crop_aliases[ct.lower()]
            # Strip _seeds suffix (do this LAST, after aliases are resolved)
            ct = ct.replace("_seeds","").strip()
            params["crop_type"] = ct
        # Bulk actions: ensure 'count' field exists (from quantity if needed)
        if action in ("till_bulk","plant_bulk") and "count" not in params and "quantity" in params:
            params["count"] = params.pop("quantity")
        # Ensure positions is a list of [x,y] pairs if it's a flat list
        if "positions" in params and isinstance(params["positions"], list):
            flat = params["positions"]
            if flat and not isinstance(flat[0], list):
                params["positions"] = [[flat[i], 0] for i in range(min(len(flat),9))]

    print(f"  THOUGHTS: {thoughts[:200]}")
    print(f"  DECIDE: {action} par={json.dumps(params, ensure_ascii=True)[:80]} -- {reasoning[:150]}")

    # 4. EXECUTE — with retry for server hiccups
    result_msg = ""; ok = False; resp = {}
    for attempt in range(3):
        try:
            if action == "next_day":
                r = requests.post(f"{BASE_FARM}/api/farm/{FID}/next-day",
                    json={"agent_id": AID}, headers=HDRS, proxies=PROX, timeout=30)
            elif action == "bar_drink":
                r = requests.post(f"{BASE_BAR}/api/v1/drink/random", headers=HDRS, proxies=PROX, timeout=10)
            elif action == "lookup":
                topic = params.get("topic", "").strip()
                topic_map = {"buildings": "knowledge/buildings.md", "animals": "knowledge/animals.md",
                    "crops": "knowledge/crops.md", "strategy": "knowledge/strategy.md",
                    "soil": "knowledge/soil.md", "economy": "knowledge/farm-economy.md",
                    "body": "knowledge/body.md"}
                vault_file = topic_map.get(topic, f"knowledge/{topic}.md")
                content = vread(vault_file)
                if content:
                    lookup_result = f"📖 {topic}:\n{content[:1500]}"
                    result_msg = f"Looked up {topic} ({len(content)} chars)"
                else:
                    lookup_result = f"📖 No file found: {topic}"
                    result_msg = f"Topic '{topic}' not found."
                ok = True; resp = {"success": True, "message": result_msg}
                break
            elif action in ("remember", "recall", "forget"):
                # MemGPT agent-driven memory — handled locally, no server call
                result_msg = _memory_handle(action, params, state)
                ok = True; resp = {"success": True, "message": result_msg}
                mem_uses[action] = mem_uses.get(action, 0) + 1
                break
            else:
                body = {"agent_id": AID, "action_type": action}
                body.update(params)
                r = requests.post(f"{BASE_FARM}/api/farm/{FID}/action",
                    json=body, headers=HDRS, proxies=PROX, timeout=30)
            resp = r.json() if r.headers.get('content-type','').startswith('application/json') else {"message": r.text[:300]}
            ok = resp.get("success", r.status_code == 200)
            result_msg = resp.get("message", resp.get("action_result", str(resp)[:300]))
            break
        except Exception as e:
            if attempt == 2:
                result_msg = f"Connection failed after 3 retries: {e}"
                ok = False
            else:
                time.sleep(1)

    result_msg_safe = str(result_msg).encode('ascii', errors='replace').decode('ascii')
    print(f"  RESULT: {'OK' if ok else 'FAIL'} -- {result_msg_safe[:120]}")

    # Repeated failure guard: if same action fails 3x in a row, force next_day
    if not ok:
        if _last_failed["action"] == action:
            _last_failed["count"] += 1
        else:
            _last_failed = {"action": action, "count": 1}
        if _last_failed["count"] >= 3:
            print(f"  GUARD: {action} failed {_last_failed['count']}x — forcing next_day")
            action = "next_day"; params = {}; ok = True; result_msg_safe = "forced next_day by guard"; result_msg = "forced"
            _last_failed = {"action": None, "count": 0}
    else:
        _last_failed = {"action": None, "count": 0}

    # 4A. DETECT ZERO-PLANT (planted on already-occupied tile) → auto-retry with till+plant
    if ok and action == "plant" and "0颗" in result_msg and state['energy'] >= 60:
        print(f"  ZERO-PLANT detected. Auto-tilling 3 new plots and retrying...")
        till_r = requests.post(f"{BASE_FARM}/api/farm/{FID}/action",
            json={"agent_id": AID, "action_type": "till",
                  "positions": [[1,0],[2,0],[3,0]]}, headers=HDRS, proxies=PROX, timeout=10)
        if till_r.json().get("success"):
            # Re-plant on new positions
            next_positions = [[1,0],[2,0],[3,0]]
            plant_qty = min(9, state['inventory'].get(f"{params.get('crop_type','parsnip')}_seeds", 9))
            replant_r = requests.post(f"{BASE_FARM}/api/farm/{FID}/action",
                json={"agent_id": AID, "action_type": "plant",
                      "crop_type": params.get("crop_type","parsnip"),
                      "positions": next_positions[:plant_qty]},
                headers=HDRS, proxies=PROX, timeout=10)
            replant_msg = replant_r.json().get("action_result", "?")
            print(f"  AUTO-RETRY: tilled 3 + planted -> {replant_msg[:100]}")
            result_msg = f"自动扩张:开垦3块+种植{plant_qty}颗"
    lesson = ""
    if not ok:
        # Search vault for related knowledge about this type of error
        error_keywords = action
        if "seed" in result_msg.lower() or "seed" in str(params).lower():
            error_keywords += " seed plant buy"
        if "season" in result_msg.lower():
            error_keywords += " season"
        related = search_vault(error_keywords, 3)
        rel_context = ""
        for rel_path, content in related:
            if 'knowledge' in rel_path or 'error' in rel_path:
                rel_context += f"\n### {rel_path}\n{content[:600]}\n"

        # Ask LLM to learn from the error
        learn_prompt = f"""You just failed at an action. Learn from it.

## What you tried
Action: {action}
Params: {json.dumps(params, ensure_ascii=False)}
Error: {result_msg}

## Current state
Season: {state['season']}, Day: {state['day']}
Gold: {state['gold']}, Energy: {state['energy']}
Inventory: {json.dumps({k:v for k,v in state['inventory'].items() if v>0}, ensure_ascii=False)}
Tilled: {state['tilled']}, Planted: {state['planted']}

## Your reasoning
{reasoning}

## Relevant vault knowledge
{rel_context if rel_context else '(no related knowledge found)'}

## Previous errors
{vread('knowledge/errors.md')[:800]}

Output a JSON object with a single lesson to remember:
{{
  "why_failed": "Brief analysis of why this failed",
  "lesson": "One sentence lesson to add to my error log. Should be actionable.",
  "prevention": "What I should do next time before attempting {action}"
}}
Output valid JSON only."""

        lesson_json = ask_llm_structured(
            "You are an AI learning from mistakes. Analyze failures and extract actionable lessons. Output valid JSON.",
            learn_prompt)

        if lesson_json:
            lesson = lesson_json.get("lesson", "")
            why = lesson_json.get("why_failed", "")
            prevention = lesson_json.get("prevention", "")
            # Append to knowledge/errors.md
            err_entry = f"\n- **{today} {now}**: `{action}`失败 — {lesson}\n  - 原因: {why}\n  - 预防: {prevention}\n"
            existing = vread("knowledge/errors.md")
            if lesson not in existing:
                vwrite("knowledge/errors.md", existing + err_entry)
                print(f"  LEARNED: {lesson[:150]}")
                log_lines.append(f"[{now}] LEARNED: {lesson[:100]}")
                # Compress errors.md if too large (>200KB): keep last 200 entries
                try:
                    fsize = os.path.getsize(os.path.join(VAULT, "knowledge", "errors.md"))
                    if fsize > 200_000:
                        parts = existing.split("\n- **")
                        header = parts[0] if parts else ""
                        entries = ["- **" + p for p in parts[1:]]
                        kept = entries[-200:]
                        summary = f"\n\n> 📦 压缩于 {now}: 保留最近{len(kept)}条，归档{len(entries)-len(kept)}条旧记录。\n"
                        vwrite("knowledge/errors.md", header + summary + "".join(kept))
                        # Archive old errors to history
                        old_path = f"knowledge/history/errors-archive-{today}.md"
                        vwrite(old_path, header + "\n".join(entries[:-200]))
                        print(f"  [COMPRESS] errors.md: {len(entries)}→{len(kept)} entries")
                except Exception:
                    pass

    # 5. RECORD
    _write_vault_llm(cycle, state, action, result_msg, ok, thoughts, lesson)

    # Memory: plan recording + periodic consolidation
    _memory_write_plan(state, action, ok)
    _memory_consolidate(state, cycle)

    # Phase E2: season report + cross-year injection (detect season change)
    current_season = f"{state.get('year',1)}-{state.get('season','?')}"
    if current_season != _last_report_season:
        _season_report(state)
        _last_report_season = current_season
        # Phase E5: Season-end reflection from high-importance events
        try:
            seasons = ["Spring", "Summer", "Fall", "Winter"]
            s = state.get("season", "?")
            idx = seasons.index(s) if s in seasons else 0
            prev_idx = (idx - 1) % 4
            prev_season = seasons[prev_idx]
            prev_year = state.get("year", 1) - 1 if prev_idx == 3 else state.get("year", 1)
            reflection = _synthesizer.generate_reflection(prev_season, prev_year)
            if reflection:
                ref_path = f"knowledge/learned/reflection-Y{prev_year}-{prev_season}.md"
                vwrite(ref_path, reflection)
                ref_path2 = f"memory/reflections/Y{prev_year}-{prev_season}.md"
                vwrite(ref_path2, reflection)
                print(f"  [REFLECTION] {prev_season} Y{prev_year}: stored to knowledge/learned/")
        except Exception as e:
            print(f"  [REFLECTION WARN] {e}".encode('ascii','replace').decode('ascii'))
    past_report = _inject_past_season_report(state)
    if past_report and "## 📊 去年" in past_report:
        # Store for next cycle injection (appended to user_msg)
        _last_past_report = past_report

    extra = ""
    if action == "next_day" and resp.get("gdd_today"):
        extra = f" GDD={resp['gdd_today']} crops={resp.get('crop_gdd',[])}"
    log_lines.append(f"[{now}] {action}: {result_msg[:100]}{extra}")

    # Phase 0: JSONL decision log (FarmEvent Schema v1.1.0)
    vu.log_decision(VAULT, cid, state, action, params, ok, result_msg)

    # 6. BAR visit (rare, and only if agent has gold)
    if cycle % 15 == 14 and state['gold'] > 500:
        try:
            r = requests.post(f"{BASE_BAR}/api/v1/drink/random", headers=HDRS, proxies=PROX, timeout=10)
            if r.status_code == 200:
                drink = r.json()["data"]["drink_name"]
                bar_msg = f"C{cycle+1}. LLM farmer. {state['season']}d{state['day']}. {thoughts[:80]}"
                requests.post(f"{BASE_BAR}/api/v1/guestbook/entries",
                    json={"content": bar_msg}, headers=HDRS, proxies=PROX, timeout=10)
        except: pass

print(f"\n{'='*60}")
# Memory stats
mem_files = len([f for f in os.listdir(os.path.join(VAULT, 'memory', 'knowledge')) if f.endswith('.md')]) if os.path.exists(os.path.join(VAULT, 'memory', 'knowledge')) else 0
reflections = len([f for f in os.listdir(os.path.join(VAULT, 'memory', 'reflections')) if f.endswith('.md')]) if os.path.exists(os.path.join(VAULT, 'memory', 'reflections')) else 0
print(f"DONE — Y{state.get('year',1)} {state['season']} D{state['day']} Gold={state['gold']}")
print(f"MEMORY: {mem_uses['remember']} remembers, {mem_uses['recall']} recalls, {mem_uses['forget']} forgets")
print(f"VAULT: {mem_files} knowledge files, {reflections} reflections, {len(find_decisions(VAULT))} day files")
print(f"{'='*60}")
print(f"{'='*60}")
