"""
decision_executor.py — Action handler + fallback engine for Agent Farm
=======================================================================
Extracted from agent-world-llm.py. Handles all 40+ action types,
parameter normalization, retry logic, and the fallback decision engine.
The core function is `execute_action()` which takes a parsed decision
dict and routes to the appropriate handler (server API, local memory,
book engine, trade engine, etc.).
"""
import requests, json, re, time, os
import vault_utils as vu
import agent_profile
import skill_tree
import knowledge_map
def execute_action(action: str, params: dict, state: dict, *,
                   cycle: int, _prof, _SKILL_TREE, VAULT: str,
                   PARENT_VAULT: str, FID: str, AID: str,
                   HDRS: dict, PROX: dict, BASE_FARM: str,
                   BASE_BAR: str, _BOOK_ENGINE, _RUMOR_ENGINE,
                   _TRADE_ENGINE, _AGENT_PROFILE_ID: str,
                   vread, vwrite, _memory_handle,
                   mem_uses: dict, _same_day_cycles: int,
                   log_lines: list) -> dict:
    """Execute one agent action. Handles all action types.
    Returns:
        dict with keys: result_msg, ok, resp, lookup_result (updated),
                        thoughts, reasoning
    """
    result_msg = ""; ok = False; resp = {}
    lookup_result = ""
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
                result_msg = _memory_handle(action, params, state)
                ok = True; resp = {"success": True, "message": result_msg}
                mem_uses[action] = mem_uses.get(action, 0) + 1
                break
            elif action == "social_msg":
                target = params.get("target", "")
                msg = params.get("message", "")
                if target and msg:
                    inbox_dir = os.path.join(PARENT_VAULT, "social")
                    os.makedirs(inbox_dir, exist_ok=True)
                    inbox_path = os.path.join(inbox_dir, f"{target}_inbox.md")
                    entry = (f"\n### {_prof.display_name} — "
                             f"{state.get('season','?')} D{state.get('day',0)} Y{state.get('year',1)}, "
                             f"🕐{state.get('hour',0):.0f}:00\n"
                             f"<!-- sender_time: {state.get('season','?')}|{state.get('day',0)}|{state.get('year',1)}|{state.get('hour',0):.1f} -->\n"
                             f"{msg}\n")
                    with open(inbox_path, "a", encoding="utf-8") as _inf:
                        _inf.write(entry)
                    result_msg = f"Sent message to {target}: {msg[:100]}"
                    target_biome = state.get("terrain_biome", [[None]*50 for _ in range(50)])
                    if isinstance(target_biome, list) and len(target_biome) > 0:
                        row = target_biome[min(5, len(target_biome)-1)]
                        bm = row[min(5, len(row)-1)] if row else "alluvial_plain"
                    else:
                        bm = "alluvial_plain"
                    kmap = knowledge_map.KnowledgeMap.load(VAULT, _prof.id)
                    rumor = _RUMOR_ENGINE.on_social_interaction(kmap, target, bm)
                    if rumor:
                        lookup_result = f"🗣 从{target}那里听到一个传言: {rumor.claim}"
                    kmap.save(VAULT)
                else:
                    result_msg = "social_msg needs target and message"
                ok = True; resp = {"success": True, "message": result_msg}
                break
            elif action == "social_lookup":
                target = params.get("target", "")
                if target:
                    lines_out = ["📋 " + target + "的农场:"]
                    try:
                        r = requests.get(f"{BASE_FARM}/api/farms", headers=HDRS, proxies=PROX, timeout=5)
                        all_farms = r.json() if r.status_code == 200 else {}
                        for fid, fdata in all_farms.items():
                            aid = fdata.get("agent_id", "")
                            tp = os.path.join(PARENT_VAULT, "agents", target, "state", "farm.md")
                            if aid and os.path.exists(tp):
                                r2 = requests.get(f"{BASE_FARM}/api/farm/{fid}", headers=HDRS, proxies=PROX, timeout=5)
                                if r2.status_code == 200:
                                    fs = r2.json()
                                    lines_out.append("  💰 金币: " + str(fs.get("gold", "?")) + "G")
                                    lines_out.append("  🌱 作物: " + str(len(fs.get("crops", []))) + "株")
                                    lines_out.append("  📦 仓库: " + str(len(fs.get("storage", []))) + "/" + str(fs.get("storage_capacity", 50)))
                                    storage = fs.get("storage", [])
                                    if storage:
                                        item_counts = {}
                                        for s in storage:
                                            name = s.get("crop_type", s.get("name", "?"))[:10]
                                            item_counts[name] = item_counts.get(name, 0) + 1
                                        storage_summary = ", ".join(k + "x" + str(v) for k, v in list(item_counts.items())[:5])
                                        lines_out.append("  🏪 库存: " + storage_summary)
                                    needs = []
                                    if fs.get("gold", 2000) < 200:
                                        needs.append("💰 缺钱")
                                    if len(fs.get("storage", [])) == 0:
                                        needs.append("🍞 缺食物")
                                    if fs.get("tilled", 0) == 0 and fs.get("gold", 2000) < 100:
                                        needs.append("🌱 缺种子")
                                    if needs:
                                        lines_out.append("  ⚠ 需求: " + ", ".join(needs))
                                break
                    except Exception:
                        pass
                    target_state_path = os.path.join(PARENT_VAULT, "agents", target, "state", "farm.md")
                    if os.path.exists(target_state_path):
                        with open(target_state_path, "r", encoding="utf-8") as _sf:
                            lines_out.append(_sf.read()[:600])
                    lookup_result = chr(10).join(lines_out)
                    result_msg = "Looked up " + target + "'s farm"
                else:
                    result_msg = "social_lookup needs target agent"
                ok = True; resp = {"success": True, "message": result_msg}
                break
            elif action == "bulletin_post":
                # Broadcast message to ALL other agents' inboxes
                msg = params.get("message", "").strip()
                if not msg:
                    result_msg = "bulletin_post needs a message"
                else:
                    all_profiles = agent_profile.list_agent_profiles(PARENT_VAULT)
                    recipients = [p for p in all_profiles if p != _AGENT_PROFILE_ID]
                    sent_count = 0
                    inbox_dir = os.path.join(PARENT_VAULT, "social")
                    os.makedirs(inbox_dir, exist_ok=True)
                    for rid in recipients:
                        try:
                            rprof = agent_profile.load_agent_profile(rid, PARENT_VAULT)
                            inbox_path = os.path.join(inbox_dir, f"{rid}_inbox.md")
                            entry = (f"\n### 📢 公告 from {_prof.display_name} — "
                                     f"{state.get('season','?')} D{state.get('day',0)} Y{state.get('year',1)}\n"
                                     f"<!-- sender_time: {state.get('season','?')}|{state.get('day',0)}|{state.get('year',1)}|{state.get('hour',0):.1f} -->\n"
                                     f"{msg}\n")
                            with open(inbox_path, "a", encoding="utf-8") as _bf:
                                _bf.write(entry)
                            sent_count += 1
                        except Exception:
                            pass
                    result_msg = f"📢 公告已发送给{sent_count}人: {msg[:80]}"
                ok = True; resp = {"success": True, "message": result_msg}
                break
            elif action == "bulletin_read":
                # Read all recent bulletin posts from social directory
                social_dir = os.path.join(PARENT_VAULT, "social")
                all_posts = []
                if os.path.isdir(social_dir):
                    for fn in os.listdir(social_dir):
                        if fn.endswith("_inbox.md"):
                            try:
                                with open(os.path.join(social_dir, fn), "r", encoding="utf-8") as _bf:
                                    content = _bf.read()
                                for line in content.split("\n"):
                                    if "📢 公告" in line:
                                        all_posts.append(line.strip())
                            except Exception:
                                pass
                if all_posts:
                    lookup_result = "## 📋 公告栏\n" + "\n".join(f"- {p}" for p in all_posts[-10:])
                    result_msg = f"Read {len(all_posts)} bulletin posts"
                else:
                    lookup_result = "## 📋 公告栏\n(还没有人发公告)"
                    result_msg = "Bulletin is empty"
                ok = True; resp = {"success": True, "message": result_msg}
                break
            elif action == "send_gift":
                target_name = params.get("target", "").strip()
                item_type = params.get("item", params.get("item_type", "")).strip()
                qty = int(params.get("qty", params.get("quantity", 1)))
                if not target_name or not item_type:
                    result_msg = "send_gift needs target and item"
                else:
                    # Send through server API — authentic transfer, not local file
                    body = {"agent_id": AID, "action_type": "send_gift",
                            "target": target_name, "item": item_type, "qty": qty}
                    r = requests.post(f"{BASE_FARM}/api/farm/{FID}/action",
                        json=body, headers=HDRS, proxies=PROX, timeout=10)
                    resp = r.json() if r.headers.get('content-type','').startswith('application/json') else {"message": r.text[:300]}
                    ok = resp.get("success", r.status_code == 200)
                    result_msg = resp.get("message", resp.get("action_result", str(resp)[:300]))
                break
            elif action == "send_gold":
                target_name = params.get("target", "").strip()
                amount = int(params.get("amount", 0))
                if not target_name or amount <= 0:
                    result_msg = "send_gold needs target and amount"
                else:
                    # Send through server API — authentic gold transfer
                    body = {"agent_id": AID, "action_type": "send_gold",
                            "target": target_name, "amount": amount}
                    r = requests.post(f"{BASE_FARM}/api/farm/{FID}/action",
                        json=body, headers=HDRS, proxies=PROX, timeout=10)
                    resp = r.json() if r.headers.get('content-type','').startswith('application/json') else {"message": r.text[:300]}
                    ok = resp.get("success", r.status_code == 200)
                    result_msg = resp.get("message", resp.get("action_result", str(resp)[:300]))
                break
            elif action == "read_book":
                book_id = params.get("book_id", "")
                if not book_id:
                    lib = _BOOK_ENGINE.get_library(_prof.id)
                    can, reason, readable = _BOOK_ENGINE.can_read(
                        _prof, lib, state.get("is_night", False),
                        has_lamp=True  # reading by firelight — no lamp needed
                    )
                    if not can:
                        result_msg = reason
                    elif readable:
                        book_id = readable[0].book_id
                    else:
                        result_msg = "没有可读的书"
                if book_id:
                    # Resolve Chinese book names to IDs (LLM uses titles like "种植基础")
                    lib = _BOOK_ENGINE.get_library(_prof.id)
                    resolved_id = _resolve_book_name(book_id, _BOOK_ENGINE, lib)
                    target_book = None
                    for b in lib:
                        if b.book_id == resolved_id:
                            target_book = b; break
                    if target_book:
                        result = _BOOK_ENGINE.read_chapter(_prof, target_book)
                        result_msg = result["message"]
                        if result.get("fact_unlocked"):
                            lookup_result = f"📖 新知识: {result['fact_unlocked']['description']}"
                    else:
                        result_msg = f"书架上没有这本书: {book_id}"
                ok = True; resp = {"success": True, "message": result_msg}
                ok = True; resp = {"success": True, "message": result_msg}
                _BOOK_ENGINE.save_library(_prof.id, lib)
                break
            elif action == "buy_book":
                book_id = params.get("book_id", "")
                book_def = _BOOK_ENGINE.get_book_def(book_id)
                if book_def and book_def.get("buy_price", 0) <= state.get("gold", 0):
                    owned = _BOOK_ENGINE.add_book_to_library(_prof.id, book_id, cycle)
                    if owned:
                        result_msg = f"买入《{book_def['title']}》（{book_def['buy_price']}G），已加入书架"
                        state["gold"] -= book_def["buy_price"]
                    else:
                        result_msg = "购买失败"
                elif not book_def:
                    result_msg = f"没找到《{book_id}》这本书"
                else:
                    result_msg = f"金币不够——{book_def['title']}要{book_def['buy_price']}G"
                ok = True; resp = {"success": True, "message": result_msg}
                break
            elif action in ("trade_propose", "trade_accept", "trade_reject", "trade_counter"):
                trade_target = params.get("target", "")
                if action == "trade_propose":
                    offer = params.get("offer", {})
                    request = params.get("request", {})
                    if isinstance(offer, str):
                        try: offer = json.loads(offer)
                        except: offer = {}
                    if isinstance(request, str):
                        try: request = json.loads(request)
                        except: request = {}
                    if not offer or not request:
                        result_msg = "trade_propose needs 'offer' and 'request' dicts"
                    else:
                        tres = _TRADE_ENGINE.propose(_prof.id, trade_target, offer, request, state)
                        result_msg = tres["message"]
                elif action == "trade_accept":
                    tid = params.get("trade_id", "")
                    if not tid:
                        result_msg = "trade_accept needs 'trade_id'"
                    else:
                        tres = _TRADE_ENGINE.accept(tid, _prof.id, state, _prof)
                        result_msg = tres["message"]
                        if tres.get("exchange"):
                            lookup_result = f"💼 交易成功！{tres['message']}"
                elif action == "trade_counter":
                    tid = params.get("trade_id", "")
                    new_offer = params.get("offer", {})
                    new_request = params.get("request", {})
                    if isinstance(new_offer, str):
                        try: new_offer = json.loads(new_offer)
                        except: new_offer = {}
                    if isinstance(new_request, str):
                        try: new_request = json.loads(new_request)
                        except: new_request = {}
                    if not tid:
                        result_msg = "trade_counter needs 'trade_id'"
                    else:
                        tres = _TRADE_ENGINE.counter(tid, _prof.id, new_offer or {}, new_request or {}, state, _prof.display_name)
                        result_msg = tres["message"]
                elif action == "trade_reject":
                    tid = params.get("trade_id", "")
                    if not tid:
                        result_msg = "trade_reject needs 'trade_id'"
                    else:
                        tres = _TRADE_ENGINE.reject(tid, _prof.id, _prof.display_name)
                        result_msg = tres["message"]
                ok = True; resp = {"success": True, "message": result_msg}
                break
            elif action == "explore":
                body = {"agent_id": AID, "action_type": "explore"}
                if "positions" in params:
                    body["positions"] = params["positions"]
                r = requests.post(f"{BASE_FARM}/api/farm/{FID}/action",
                    json=body, headers=HDRS, proxies=PROX, timeout=30)
                resp = r.json() if r.headers.get('content-type','').startswith('application/json') else {"message": r.text[:300]}
                ok = resp.get("success", r.status_code == 200)
                result_msg = resp.get("message", resp.get("action_result", str(resp)[:300]))
                if ok and resp.get("discovery"):
                    disc = resp["discovery"]
                    dist = abs(disc.get("x", 0)) + abs(disc.get("y", 0))
                    dt = 1 if dist <= 2 else (3 if dist <= 5 else 8)
                    kmap = knowledge_map.KnowledgeMap.load(VAULT, _prof.id)
                    kmap.observe_tile(disc.get("x", 0), disc.get("y", 0), dt, {
                        "biome": disc.get("biome", ""), "soil_type": disc.get("soil_type", ""),
                        "soil_moisture": disc.get("soil_moisture", 50),
                        "topsoil_depth": disc.get("topsoil_depth", 20),
                        "water_nearby": disc.get("water_nearby", False),
                        "revealed_resource": disc.get("resource", ""),
                    }, cycle=0)
                    kmap.save(VAULT)
                    _prof.history["tiles_explored"] = _prof.history.get("tiles_explored", 0) + 1
                    reality = {
                        "biome": disc.get("biome", ""),
                        "resource": disc.get("resource", ""),
                        "water_nearby": disc.get("water_nearby", False),
                    }
                    rumor_msgs = _RUMOR_ENGINE.on_explore(
                        kmap, disc.get("x", 0), disc.get("y", 0),
                        disc.get("biome", "alluvial_plain"), reality
                    )
                    if rumor_msgs:
                        lookup_result = "\n".join(rumor_msgs)
                    kmap.save(VAULT)
                break
            else:
                # All other actions: forward to server
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
    return {
        "result_msg": result_msg, "ok": ok, "resp": resp,
        "lookup_result": lookup_result, "action": action, "params": params,
    }


# ═══════════════════════════ BOOK NAME RESOLUTION ═══════════════════════════

def _resolve_book_name(book_id: str, _BOOK_ENGINE, library: list) -> str:
    """Resolve Chinese book titles to English IDs. LLM passes titles like '种植基础',
    '锻造入门' etc. but actual book files use English IDs like 'planting_basics'."""
    # Direct match first
    if _BOOK_ENGINE.get_book_def(book_id):
        return book_id
    # Check library for title match
    for b in library:
        if b.title and b.title in book_id:
            return b.book_id
    # Check catalog for title match
    all_books = _BOOK_ENGINE.list_available_books()
    for b in all_books:
        if b.get("title", "") in book_id:
            return b["id"]
        if book_id in b.get("title", ""):
            return b["id"]
    # Known Chinese→English mappings
    TITLE_MAP = {
        "种植基础": "planting_basics", "畜牧基础": "herding_basics",
        "锻造入门": "forging_basics", "小麦种植指南": "wheat_guide",
        "牧场经济学": "pasture_economics", "不确定性的礼物": "uncertainty_gift",
        "三年大旱纪": "great_drought", "鲁滨逊漂流记": "robinson_crusoe",
        "土壤的科学": "soil_science", "锻造之道": "forge_mastery",
        "草木药典": "herbal_medicine", "治水要略": "water_wisdom",
        "农夫的日记": "farmer_diary_template",
    }
    for cn_name, eng_id in TITLE_MAP.items():
        if cn_name in book_id or book_id in cn_name:
            return eng_id
    return book_id  # fallback: return as-is


# ═══════════════════════════ PARAM NORMALIZATION ═══════════════════════════
def normalize_params(params: dict) -> dict:
    """Normalize LLM-invented parameter names to server-accepted keys."""
    if not params:
        return {}
    for old_key, new_key in [("item","crop_type"),("seed","crop_type"),("crop","crop_type"),
                               ("pos","positions"),("position","positions"),
                               ("amount","quantity"),("count","count"),("qty","quantity"),
                               ("quantity","amount"),  # send_gold/send_gift: LLM uses quantity
                               ("type","building_type"),("building","building_type")]:
        if old_key in params and new_key not in params:
            params[new_key] = params.pop(old_key)
    if "crop_type" in params:
        ct = str(params["crop_type"]).strip()
        ct = ct.replace('"','').replace("'","").strip()
        crop_aliases = {
            "winter":"winter_seeds", "winter seed":"winter_seeds",
            "powder":"powder_melon", "melon seed":"melon",
            "parsnip seed":"parsnip","wheat seed":"wheat","corn seed":"corn",
            "pumpkin seed":"pumpkin","soybean seed":"soybean","tomato seed":"tomato",
        }
        if ct.lower() in crop_aliases: ct = crop_aliases[ct.lower()]
        ct = ct.replace("_seeds","").strip()
        params["crop_type"] = ct
    if "positions" in params and isinstance(params["positions"], list):
        flat = params["positions"]
        if flat and not isinstance(flat[0], list):
            params["positions"] = [[flat[i], 0] for i in range(min(len(flat),9))]
    return params
# ═══════════════════════════ FALLBACK ENGINE ═══════════════════════════
def fallback_decision(state: dict, CROPS: dict) -> dict:
    """Rule-based fallback when LLM fails to respond. Returns {action, params, thoughts}."""
    action = "next_day"; params = {}
    crops = state.get("crops", []); storage = state.get("storage", [])
    gold = state["gold"]; farmer = state.get("farmer", {})
    inv = state.get("inventory", {})
    empty_tiles = state["tilled"] - state["planted"]
    hour = state.get("hour", 7.0)
    is_night = state.get("is_night", False)
    if is_night:
        action = "sleep"; params = {"hours": 8}
        return {"action": action, "params": params, "thoughts": "Night — sleep to morning"}
    # ── Daytime fallback logic ──
    if len(storage) > 2 and gold < 5000:
        action = "sell_storage"; thoughts = f"Sell ({len(storage)} items, G={gold})"
    elif any(c.get("gdd_percent",0) >= 95 for c in crops):
        action = "harvest"; thoughts = "Harvest ripe"
    elif any(not c.get("watered_today",False) for c in crops) and state["weather"] not in ("rainy","flood"):
        pts = [[c.get("position_x",0),c.get("position_y",0)] for c in crops if not c.get("watered_today",False)]
        action = "water"; params = {"positions": pts}; thoughts = "Water crops"
    elif farmer.get("hunger",100) < 50 and len(storage) > 0:
        action = "eat"; thoughts = f"Eat (hunger={farmer['hunger']})"
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
    if "fence" not in state.get("buildings",[]) and gold >= 2100:
        action = "build"; params = {"building_type": "fence"}; thoughts = "Build fence"
    elif "well" not in state.get("buildings",[]) and gold >= 3100 and "fence" in state.get("buildings",[]):
        action = "build"; params = {"building_type": "well"}; thoughts = "Build well"
    if action == "next_day" and state.get("weed_count",0) > 5 and state["energy"] >= 30:
        action = "weed_all"; thoughts = f"Weed ({state['weed_count']} weeds)"
    if action == "next_day" and state["tilled"] < 9 and state["energy"] >= 50 and (state["tilled"] == 0 or gold >= 200):
        farm_lv = farmer.get("skills",{}).get("farming",1)
        need = 9 - state["tilled"]
        if farm_lv >= 2 and need >= 4:
            n = min(need, 6)
            action = "till_bulk"; params = {"count": n}
            thoughts = f"Bulk till {n}"
        else:
            action = "till"; params = {"positions": [[0,0],[0,1],[0,2]]}
            thoughts = f"Till 3"
    if action == "next_day" and gold >= 100:
        for cn, ci in CROPS.items():
            if state["season"] in ci["seasons"]:
                sk = f"{cn}_seeds"
                have = inv.get(sk,0)
                if have < empty_tiles + 3:
                    qty = min(10, max(3, (gold - 500) // max(1, ci["buy"])))
                    qty = max(3, qty)
                    if qty >= 3 and gold >= ci["buy"] * qty:
                        action = "buy"; params = {"item_type": cn, "quantity": qty}
                        thoughts = f"Buy {qty}x{ci['name']}"; break
    if action == "next_day" and farmer.get("hunger",100) < 60 and len(storage) == 0 and gold >= 30:
        action = "buy"; params = {"item_type": "bread"}; thoughts = "Buy bread"
    if action == "next_day" and farmer.get("fatigue",0) >= 30:
        action = "sleep"; params = {"hours": 8}; thoughts = "Tired — sleep"
    return {"action": action, "params": params or {}, "thoughts": "Fallback"}
