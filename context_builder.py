"""
context_builder.py — Build LLM user message from farm state
=============================================================
Extracted from agent-world-llm.py. Takes the full farm state dict
and produces the user_message string sent to DeepSeek each cycle.
"""
import os, json, re, requests
import trade_engine


def build_user_message(state, *, cycle: int, log_lines: list,
                       lookup_result: str, _prof, PARENT_VAULT: str,
                       VAULT: str, interrupt_context: dict,
                       _same_day_cycles: int, CROPS: dict,
                       _last_past_report: str, _last_failed: dict,
                       _BOOK_ENGINE, _RUMOR_ENGINE, _TRADE_ENGINE,
                       _AGENT_PROFILE_ID: str, _memory_retrieve,
                       vread, agent_profile, knowledge_map,
                       HDRS: dict, PROX: dict, BASE_FARM: str,
                       BASE_BAR: str, FID: str, AID: str) -> dict:
    """Build the complete user_message for one agent cycle.

    Returns:
        dict with keys: user_msg, lookup_result (updated), _last_past_report (updated)
    """
    # ── Build shop ──
    current_season = state.get("season", "Spring")
    shop_items = []
    # Initialize variables that may not be set in all code paths
    new_lookup_result = ""
    
    # ── Shared world threats (same for all agents on same day) ──
    day_seed = state.get("year",1)*1000 + state.get("day",1)*10 + {"Spring":1,"Summer":2,"Fall":3,"Winter":4}.get(state.get("season","Spring"),1)
    import hashlib, random as _random
    _rng = _random.Random(int(hashlib.md5(f"threat_{day_seed}".encode()).hexdigest()[:8], 16))
    threat_roll = _rng.random()
    shared_threat_msg = ""
    if threat_roll < 0.25:  # 25% chance per day of a shared threat
        threats = [
            "🐺 狼群警报！狼群正在逼近家族农场——围栏是唯一的防线。用 bulletin_post 通知全家人！",
            "🌊 洪水预警！上游大坝裂缝，低洼地可能被淹。立刻告诉家人——祖父的治疗费不能毁于一场洪水。",
            "🐰 野兔泛滥！今年春天异常温暖，野兔繁殖速度翻倍。单靠一个人挡不住——全家人必须一起建围栏。",
            "🦅 鹰群来袭！小鸡和幼畜危险。用 bulletin_post 协调全家的防御策略——每一只牲畜都是祖父的医药费。",
        ]
        shared_threat_msg = f"## ⚠️ 家族危机 — 影响全家人\n{_rng.choice(threats)}\n\n"
    
    # ── Build shop ──
    for k, c in CROPS.items():
        if current_season in c.get("seasons", []):
            shop_items.append(f"SEED:{k}(GDD={c['gdd_req']},buy={c['buy']},sell={c['sell']})")
    shop_list = f"ONLY plant these in {current_season}:\n" + "\n".join(shop_items) if shop_items else "(no seasonal crops)"

    food_list = ""
    try:
        r = requests.get(f"{BASE_FARM}/api/market/prices", headers=HDRS, proxies=PROX, timeout=5)
        foods = r.json().get("foods", {})
        for k, v in sorted(foods.items()):
            food_list += f"[食品]{k}({v['price']}G, 保质{v['freshness']}d) "
    except Exception:
        pass
    shop_list += "\n\n[食品店] " + food_list

    # ── Dashboard ──
    fm = state.get("farmer", {})
    crop_count = len(state["crops"])
    empty_tiles = state["tilled"] - state["planted"]
    storage_used = len(state.get("storage", []))
    storage_cap = state.get("storage_capacity", 50)
    fitness = fm.get("fitness", 1.0)
    knowledge = fm.get("knowledge", {})
    top_knowledge = max(knowledge.items(), key=lambda x: x[1]) if knowledge else ("无", 0)

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

    user_msg = dashboard + f"Inv: {json.dumps({k:v for k,v in state.get('inventory',{}).items() if v>0}, ensure_ascii=False)}"
    # Shared threats (25% chance per day)
    user_msg += shared_threat_msg
    # Community status reminder
    user_msg += "\n👨‍👩‍👦 家族: 续仁武、铁娘子、老王是一家人 | 目标: 攒钱给祖父治病 | bulletin_post | bulletin_read | social_msg\n"

    # Livestock
    if state.get("livestock"):
        ls = state["livestock"]
        alines = [f"{a['name']}({a['species']} hp{a['health']}{' READY' if a.get('product_ready') else ''}{' HUNGRY' if not a.get('fed_today') else ''})" for a in ls]
        user_msg += f" | Animals({len(ls)}): {', '.join(alines)}"
        user_msg += f" | Manure: {state.get('manure_stockpile',0):.1f}"
    user_msg += "\n"

    # Crops
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

    # ── Observations ──
    obs_lines = []
    if len(log_lines) > 10 and _last_failed["count"] >= 2:
        obs_lines.append("🧠 已经连续失败了——用 remember 记录教训!")

    ripe = [c for c in state['crops'] if c.get('gdd_percent',0) >= 95]
    if ripe:
        names = ", ".join(f"{c.get('crop_name',c.get('crop_type','?'))}({c.get('gdd_percent',0)}%)" for c in ripe)
        obs_lines.append(f"🔥 成熟作物: {names} — 必须立刻 harvest! 不收会腐烂!")
    near = [c for c in state['crops'] if 50 <= c.get('gdd_percent',0) < 95]
    if near:
        obs_lines.append(f"🌱 生长中: {len(near)}株(GDD 50-95%)")
    empty = state['tilled'] - state['planted']
    seeds = {k:v for k,v in state.get('inventory',{}).items() if v>0 and '_seeds' in k}
    if empty > 0 and not seeds:
        obs_lines.append(f"⛏ 空闲土地: {empty}格 — 需要买种子!")
    elif empty > 0 and seeds:
        seed_names = ", ".join(f"{k}({v}颗)" for k,v in list(seeds.items())[:3])
        obs_lines.append(f"🌱 你手里有种子({seed_names}), {empty}块空地等着你——快 plant!")
    elif state['tilled'] == 0 and seeds:
        seed_names = ", ".join(f"{k}({v}颗)" for k,v in list(seeds.items())[:3])
        obs_lines.append(f"⚠ 你有{seed_names}但还没翻地——立刻 till 开垦!")
    if state['tilled'] == 0:
        obs_lines.append("⛏ 没有任何翻耕地！用 till 开垦土地 → 然后 plant 播种 → 最后 water 浇水")
    dry = [c for c in state['crops'] if not c.get("watered_today", False)]
    if dry:
        obs_lines.append(f"💧 {len(dry)}株未浇水")
    wc = state.get('weed_count', 0)
    if wc > 5:
        obs_lines.append(f"🌿 {wc}块地长满杂草!")
    elif wc > 0:
        obs_lines.append(f"🌿 {wc}块地有杂草")
    sl = len(state.get('storage', []))
    if sl > 5:
        obs_lines.append(f"📦 仓库{sl}件 — sell_storage 换金币(你的收入)")
    elif sl > 0:
        obs_lines.append(f"📦 仓库有{sl}件 — sell_storage 换金币(你的收入)")
    if state['gold'] < 200:
        obs_lines.append(f"💰 金币将尽({state['gold']}G) — sell 换钱!")
    elif state['gold'] > 3000:
        obs_lines.append(f"💰 金币充裕({state['gold']}G) — 可建造")
    if not seeds and state['tilled'] > 0 and empty > 0:
        obs_lines.append("⚠ 缺少种子 — 用 buy 买一些!")
    if state['weather'] in ('frost',):
        obs_lines.append("🧊 霜冻天气")
    if state['weather'] in ('drought',):
        obs_lines.append("🏜 干旱天气")

    if obs_lines:
        user_msg += "## 📋 农场观察\n" + "\n".join(obs_lines) + "\n"

    # ── Ecology observations ──
    eco_obs = state.get("ecology_observations", [])
    if eco_obs:
        user_msg += "## 🦊 野生动物观察\n" + "\n".join(f"- {o}" for o in eco_obs[:4]) + "\n"
    wolf_w = state.get("wolf_warning", "")
    if wolf_w:
        user_msg += f"🐺 {wolf_w}\n"
    eco_alerts = state.get("ecology_alerts", [])
    if eco_alerts:
        user_msg += "## ⚠ 生态警报\n"
        for a in eco_alerts[-3:]:
            user_msg += f"- {a.get('description', '?')}\n"

    # ── Sensory ──
    sensory = state.get("sensory_observations", [])
    if sensory:
        user_msg += "## 🔍 感官感知\n" + "\n".join(f"- {s}" for s in sensory[:8]) + "\n"
    user_msg += "\n→ Output valid JSON. Keep thoughts < 80 chars."

    if lookup_result:
        user_msg += f"\n{lookup_result}\n"
        lookup_result = ""

    if _last_past_report:
        user_msg += f"\n{_last_past_report}\n"
        _last_past_report = ""

    mem_context = _memory_retrieve(state)
    if mem_context:
        user_msg += f"\n{mem_context}\n"
    user_msg += "\n→ Output valid JSON. Keep thoughts < 80 chars."

    # ── Social awareness ──
    hour = state.get("hour", 7.0)
    is_mail_time = (21.0 <= hour < 24.0)  # 21:00 is mail check time
    
    try:
        # Check inbox under both profile ID and display name (LLM may use either)
        inbox_candidates = [
            os.path.join(PARENT_VAULT, "social", f"{_prof.id}_inbox.md"),
            os.path.join(PARENT_VAULT, "social", f"{_prof.display_name}_inbox.md"),
        ]
        inbox_path = None
        for cand in inbox_candidates:
            if os.path.exists(cand) and os.path.getsize(cand) > 0:
                inbox_path = cand; break
        # Fallback: first candidate
        if inbox_path is None:
            inbox_path = inbox_candidates[0]
        if os.path.exists(inbox_path):
            with open(inbox_path, "r", encoding="utf-8") as _ibf:
                inbox_content = _ibf.read()
            if inbox_content.strip():
                import re as _re
                _time_match = _re.search(r'<!-- sender_time: (\w+)\|(\d+)\|(\d+)\|([\d.]+) -->', inbox_content)
                time_note = ""
                new_lookup_result = ""
                if _time_match:
                    s_season, s_day, s_year, s_hour = _time_match.group(1), int(_time_match.group(2)), int(_time_match.group(3)), float(_time_match.group(4))
                    my_season, my_day, my_year = state.get("season","?"), state.get("day",0), state.get("year",1)
                    seasons = ["Spring", "Summer", "Fall", "Winter"]
                    try:
                        sender_pos = s_year * 4 + seasons.index(s_season)
                        my_pos = my_year * 4 + seasons.index(my_season)
                        season_diff = my_pos - sender_pos
                        if season_diff == 0:
                            day_diff = my_day - s_day
                            time_note = f" ({day_diff}天前)" if day_diff > 1 else (" (昨天)" if day_diff == 1 else " (几小时前)")
                        elif season_diff == 1:
                            time_note = f" (上季末)"
                        else:
                            time_note = f" ({season_diff}季前, {s_season} Y{s_year})"
                    except Exception:
                        pass

                # Highlight inbox at mail time (21:00), regular otherwise
                if is_mail_time:
                    user_msg += f"## 📬 收信时间 (21:00) — 你有新消息{time_note}\n{inbox_content[-600:]}\n"
                    user_msg += "💡 现在是晚间收信时间。你可以回复消息(social_msg)、查看留言栏(bulletin_read)、或发布公告(bulletin_post)。\n"
                else:
                    user_msg += f"## 💬 收到的消息{time_note}\n{inbox_content[-600:]}\n"

                sender_line = [l for l in inbox_content.split("\n") if l.startswith("### ") and " — " in l]
                sender_name = sender_line[0].split("### ")[1].split(" — ")[0].strip() if sender_line else "someone"
                msg_lines = [l for l in inbox_content.split("\n") if l.strip() and not l.startswith("<!--") and not l.startswith("###")]
                msg_body = " ".join(msg_lines).strip()[:200]
                if msg_body:
                    new_lookup_result = f"🗣 从{sender_name}收到消息并记住: {msg_body[:100]}"
                open(inbox_path, "w", encoding="utf-8").close()
    except Exception:
        pass
    
    # At 21:00 mail check time, always remind about social options
    if is_mail_time:
        user_msg += "## 📬 家族收信时间 (21:00)\n"
        user_msg += "你可以：bulletin_post(公告) | bulletin_read(读公告栏) | social_msg(私信家人) | social_lookup(查看家人农场)\n"
        user_msg += "💡 记得——每一枚金币都是给祖父的。和家人商量分工，不要各自为战。\n"

    # Periodic family nudge during daytime (every ~15 cycles)
    if cycle % 15 == 0 and not is_mail_time and hour >= 6:
        user_msg += "## 👨‍👩‍👦 别忘了你的家人\n"
        user_msg += "铁娘子和老王也在为祖父的治疗费努力。如果你有富余的种子或金币，问问他们是否需要。\n"
        user_msg += "如果你遇到了困难——说出来。家人之间不需要逞强。\n"

    try:
        all_profiles = agent_profile.list_agent_profiles(PARENT_VAULT)
        others = [p for p in all_profiles if p != _AGENT_PROFILE_ID]
        if others:
            other_names = []
            for oid in others:
                op = agent_profile.load_agent_profile(oid, PARENT_VAULT)
                other_names.append(f"{op.display_name}({op.role})")
            user_msg += f"## 👨‍👩‍👦 你的家人\n{', '.join(other_names)}\n"
            user_msg += "(你们是一家人，共享这片祖父留下的土地。social_msg 私聊，social_lookup 查看他们的农场状态)\n"
    except Exception as _e:
        print(f"  [SOCIAL WARN] {str(_e)[:80]}".encode('ascii','replace').decode('ascii'))

    try:
        pending = _TRADE_ENGINE.list_pending_for(_prof.id)
        if pending:
            user_msg += "## 💼 待处理的交易提议\n"
            for t in pending[:2]:
                user_msg += f"- 来自 **{t.from_agent}**：用 **{trade_engine.TradeEngine._format_items(t.offer)}** 换 **{trade_engine.TradeEngine._format_items(t.request)}** (ID: `{t.trade_id}`)\n"
            user_msg += "回复: `trade_accept(trade_id)` / `trade_counter(...)` / `trade_reject(trade_id)`\n"
    except Exception:
        pass

    try:
        lib = _BOOK_ENGINE.get_library(_prof.id)
        progress = _BOOK_ENGINE.reading_progress_snippet(lib)
        if progress:
            user_msg += progress + "\n"
        if is_night:
            can, reason, readable = _BOOK_ENGINE.can_read(_prof, lib, is_night)
            if can and readable:
                next_book = readable[0]
                user_msg += f"🌙 夜晚无事——书架上还有未读完的《{next_book.title}》（已读{next_book.chapters_read}/{next_book.chapters}章）。可以用 read_book 阅读。\n"
    except Exception:
        pass

    try:
        kmap = knowledge_map.KnowledgeMap.load(VAULT, _prof.id)
        rumor_snippet = kmap.unverified_rumors_snippet()
        if rumor_snippet:
            user_msg += rumor_snippet + "\n"
        if cycle % 30 == 0:
            current_biome = state.get("terrain_biome", None)
            if isinstance(current_biome, list) and len(current_biome) > 0:
                row = current_biome[min(5, len(current_biome)-1)]
                bm = row[min(5, len(row)-1)] if row else "alluvial_plain"
            else:
                bm = "alluvial_plain"
            rumor = _RUMOR_ENGINE.idle_reflection(kmap, bm)
            if rumor:
                user_msg += f"\n💭 你忽然想到: {rumor.claim}\n"
            kmap.save(VAULT)
    except Exception:
        pass

    return {"user_msg": user_msg, "lookup_result": lookup_result,
            "_last_past_report": _last_past_report,
            "new_lookup_result": new_lookup_result}
