"""
vault_utils.py — Shared vault I/O + Phase 0 decision log layer
===============================================================
Used by agent-world-llm.py and agent-world-brain.py.
Provides normalized cross-platform vault read/write and
Phase 0 JSONL decision logging infrastructure.

v1.1.0 — FarmEvent Schema with importance_score, tags, context_before, outcome.
"""
import os, json, datetime


def vwrite(vault_root, path, content):
    """Write content to a vault file. Creates directories as needed.
    Normalizes forward slashes to OS separator for cross-platform safety."""
    clean = path.replace('/', os.sep)
    full = os.path.join(vault_root, clean)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w', encoding='utf-8') as f:
        f.write(content)


def vread(vault_root, path):
    """Read a vault file. Returns '' if file doesn't exist."""
    clean = path.replace('/', os.sep)
    try:
        with open(os.path.join(vault_root, clean), 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        return ''


def vappend(vault_root, path, content):
    """Append content to a vault file. Creates dirs if needed."""
    clean = path.replace('/', os.sep)
    full = os.path.join(vault_root, clean)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'a', encoding='utf-8') as f:
        f.write(content)


def search_vault(vault_root, keyword, n=5):
    """Full-text search across all .md files in vault. Returns [(rel_path, excerpt), ...]."""
    results = []
    for root, _, fns in os.walk(vault_root):
        for fn in fns:
            if not fn.endswith('.md'):
                continue
            full = os.path.join(root, fn)
            try:
                with open(full, 'r', encoding='utf-8') as f:
                    content = f.read()
                if keyword.lower() in content.lower():
                    rel = os.path.relpath(full, vault_root).replace('\\', '/')
                    first_line = content.strip().split('\n')[0].lstrip('# ')
                    results.append((rel, content[:800]))
            except Exception:
                pass
    return results[:n]


# ═══════════════════════════ FarmEvent Schema (v1.1.0) ═══════════════════════════

DECISION_LOG_FILE = "decisions/decision_log.jsonl"

# ── Event type taxonomy ──
# Maps action → event_type prefix for structured querying.
ACTION_TO_EVENT_TYPE = {
    # Core farming
    "till": "action_till", "till_bulk": "action_till_bulk",
    "plant": "action_plant", "plant_bulk": "action_plant_bulk", "plant_tree": "action_plant_tree",
    "water": "action_water",
    "harvest": "action_harvest",
    "weed_all": "action_weed_all",
    "fertilize": "action_fertilize", "compost": "action_compost",
    "apply_compost": "action_apply_compost", "green_manure": "action_green_manure",
    "mulch": "action_mulch", "lime": "action_lime", "sulfur": "action_sulfur",
    "save_seeds": "action_save_seeds",
    # Drainage + irrigation
    "drain": "action_drain", "drain_bulk": "action_drain_bulk",
    "irrigate_drip": "action_irrigate_drip",
    "irrigate_flood": "action_irrigate_flood",
    "irrigate_sprinkler": "action_irrigate_sprinkler",
    # Economy
    "buy": "economic_transaction", "sell_storage": "economic_transaction",
    "buy_animal": "economic_transaction", "buy_tool": "economic_transaction",
    "buy_material": "economic_transaction",
    "loan": "economic_transaction", "repay_loan": "economic_transaction",
    "sign_contract": "economic_transaction", "deliver_contract": "economic_transaction",
    "cancel_insurance": "economic_transaction",
    # Construction
    "build": "action_build", "propose_building": "action_propose_building",
    # Tools
    "forge": "action_forge", "repair": "action_repair",
    # Livestock
    "feed_animals": "action_feed_animals", "water_animals": "action_water_animals",
    "collect_products": "action_collect_products", "treat_animal": "action_treat_animal",
    "breed": "action_breed", "slaughter": "action_slaughter",
    "send_to_pasture": "action_send_to_pasture", "bring_to_shelter": "action_bring_to_shelter",
    "isolate": "action_isolate",
    # Body
    "eat": "action_eat", "drink_water": "action_drink_water",
    "drink_coffee": "action_drink_coffee", "sleep": "action_sleep",
    "exercise": "action_exercise", "read": "action_read", "research": "action_research",
    # Processing
    "process": "action_process", "process_food": "action_process_food",
    # Meta
    "next_day": "meta_day_advance", "lookup": "meta_lookup",
    "remember": "meta_memory_store", "recall": "meta_memory_retrieve",
    "forget": "meta_memory_forget",
    "move": "action_move", "bury": "action_bury", "fell_tree": "action_fell_tree",
    "prune": "action_prune",
    "spread_manure": "action_spread_manure",
    "insurance": "meta_insurance",
}

# ── Auto-tagging: action → default tags ──
ACTION_TAGS = {
    "till": ["tillage"], "plant": ["planting"], "water": ["irrigation"],
    "harvest": ["harvest"], "sell_storage": ["economy", "sell"],
    "buy": ["economy", "buy"], "build": ["construction"],
    "feed_animals": ["livestock"], "treat_animal": ["livestock"],
    "drain": ["drainage"], "fertilize": ["soil"], "compost": ["soil"],
    "save_seeds": ["genetics"], "breed": ["genetics", "livestock"],
    "sleep": ["body"], "eat": ["body"], "exercise": ["body"],
    "next_day": ["meta"], "remember": ["memory"], "recall": ["memory"],
}


def log_decision(vault_root, decision_id, state, action, params,
                 success, result_msg, **kwargs):
    """Append one structured FarmEvent entry to the JSONL log.

    FarmEvent Schema v1.1.0 — extended fields beyond the original flat format.

    Args:
        vault_root:   Path to vault root directory.
        decision_id:  Unique event ID string.
        state:        Full farm state dict from API (includes farmer, crops, storage...).
        action:       Action string (e.g. "harvest", "plant").
        params:       Action parameters dict.
        success:      Whether the action succeeded.
        result_msg:   Result message string from server.

    Keyword args (new in v1.1.0):
        importance_score: 0-10, assigned by importance scorer (default 0). 0=not yet scored.
        tags:             List of tag strings for search/retrieval.
        context_before:   Dict of relevant state values before the action.
        outcome:          Dict of structured outcome data (yield, quality, cost...).
        extra:            Any additional unstructured metadata.
    """
    now = datetime.datetime.now()

    # ── Build context_before if not provided ──
    context_before = kwargs.get("context_before")
    if context_before is None:
        context_before = _build_context_before(state, action)

    # ── Build outcome if not provided ──
    outcome = kwargs.get("outcome")
    if outcome is None:
        outcome = _build_outcome(state, action, params, success, result_msg)

    # ── Build tags ──
    tags = kwargs.get("tags", [])
    if not tags and action in ACTION_TAGS:
        tags = list(ACTION_TAGS[action])
    # Auto-tag with season + weather
    season = state.get("season", "?")
    weather = state.get("weather", "?")
    if season not in tags:
        tags.append(season.lower())
    if weather not in ("?", "sunny"):
        tags.append(weather)

    entry = {
        # ── Core identity ──
        "id": decision_id,
        "timestamp": now.isoformat(),
        "event_type": ACTION_TO_EVENT_TYPE.get(action, f"action_{action}"),

        # ── Time ──
        "season": season,
        "day": state.get("day", 0),
        "year": state.get("year", 1),
        "hour": state.get("hour", -1),

        # ── Agent + action ──
        "agent_id": "farmer_01",
        "action": action,
        "params": params,

        # ── State snapshot (compact, for quick retrieval) ──
        "state_snippet": (
            f"gold={state.get('gold',0)} energy={state.get('energy',0)} "
            f"tilled={state.get('tilled',0)} planted={state.get('planted',0)} "
            f"weather={weather}"
        ),

        # ── Outcome ──
        "success": success,
        "result": str(result_msg)[:200],
        "error": None if success else str(result_msg)[:200],
        "outcome": outcome,

        # ── Context (v1.1.0) ──
        "context_before": context_before,
        "importance_score": kwargs.get("importance_score", 0),
        "tags": tags,

        # ── Extensibility ──
        "gcg": kwargs.get("gcg_metrics", {}),
        "extra": kwargs.get("extra", {}),
    }
    full = os.path.join(vault_root, DECISION_LOG_FILE)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def _build_context_before(state, action):
    """Build a structured context snapshot for the given action type."""
    ctx = {
        "gold": state.get("gold", 0),
        "energy": state.get("energy", 0),
        "weather": state.get("weather", "?"),
        "season": state.get("season", "?"),
        "hour": state.get("hour", -1),
        "tilled": state.get("tilled", 0),
        "planted": state.get("planted", 0),
    }
    farmer = state.get("farmer", {})
    if farmer:
        ctx["hunger"] = farmer.get("hunger", 100)
        ctx["fatigue"] = farmer.get("fatigue", 0)
        ctx["sleepiness"] = farmer.get("sleepiness", 0)

    # Action-specific context
    if action == "harvest":
        ctx["near_harvest_crops"] = sum(
            1 for c in state.get("crops", [])
            if c.get("gdd_percent", 0) >= 95
        )
    elif action == "plant":
        ctx["empty_tiles"] = state.get("tilled", 0) - state.get("planted", 0)
        ctx["seed_count"] = sum(
            v for k, v in state.get("inventory", {}).items()
            if v > 0 and "_seeds" in k
        )
    elif action == "sell_storage":
        ctx["storage_count"] = len(state.get("storage", []))
    elif action == "buy":
        ctx["gold_before"] = state.get("gold", 0)
    elif action == "build":
        ctx["building_count"] = len(state.get("buildings", []))

    return ctx


def _build_outcome(state, action, params, success, result_msg):
    """Build a structured outcome dict from the result message and context."""
    outcome = {"raw": str(result_msg)[:200]}

    if not success:
        return outcome

    if action == "harvest":
        # Try to extract yield information
        result_s = str(result_msg)
        outcome["action"] = "harvest"
        if "S级" in result_s:
            outcome["quality"] = "S"
        elif "A级" in result_s:
            outcome["quality"] = "A"
        elif "C级" in result_s:
            outcome["quality"] = "C"
        # Extract value if present
        if "value" in result_s.lower() or "G" in result_s:
            import re as _re
            gold_match = _re.search(r'(\d+)G', result_s)
            if gold_match:
                outcome["estimated_value"] = int(gold_match.group(1))

    elif action == "sell_storage":
        result_s = str(result_msg)
        import re as _re
        gold_match = _re.search(r'(\d+)G', result_s)
        if gold_match:
            outcome["revenue"] = int(gold_match.group(1))

    elif action == "buy":
        crop_type = params.get("crop_type", "")
        quantity = params.get("quantity", 0)
        if crop_type:
            outcome["purchased"] = f"{quantity}x {crop_type}"

    elif action == "plant":
        crop_type = params.get("crop_type", "")
        positions = params.get("positions", [])
        outcome["planted"] = f"{len(positions)}x {crop_type}"

    elif action == "build":
        building_type = params.get("building_type", "")
        outcome["building"] = building_type

    elif action == "sleep":
        outcome["recovered"] = "energy + fatigue"

    elif action == "next_day":
        outcome["advanced"] = True

    return outcome


# ═══════════════════════════ Legacy-compatible loaders ═══════════════════════════

def load_recent_decisions(vault_root, n=10):
    """Return the last N decision entries from the JSONL log.
    Returns [] if log doesn't exist or is empty."""
    log_path = os.path.join(vault_root, DECISION_LOG_FILE)
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception:
        return []
    entries = []
    for line in lines[-n:]:
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line.strip()))
        except json.JSONDecodeError:
            continue
    return entries


def load_decisions_for_season(vault_root, season, year):
    """Return all decisions for a specific season + year."""
    log_path = os.path.join(vault_root, DECISION_LOG_FILE)
    if not os.path.exists(log_path):
        return []
    entries = []
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line.strip())
                    if entry.get("season") == season and entry.get("year") == year:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    return entries


def load_decisions_for_day_range(vault_root, season, year, day_start, day_end):
    """Return decisions within a specific day range. Used by retrieve_similar_days()."""
    all_in_season = load_decisions_for_season(vault_root, season, year)
    return [d for d in all_in_season
            if day_start <= d.get("day", 0) <= day_end]


def load_important_decisions(vault_root, min_score=5, n=50):
    """Return recent high-importance decisions (for long-term memory injection)."""
    log_path = os.path.join(vault_root, DECISION_LOG_FILE)
    if not os.path.exists(log_path):
        return []
    entries = []
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line.strip())
                    if entry.get("importance_score", 0) >= min_score:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    # Return most recent N
    return entries[-n:]


def load_decisions_by_tag(vault_root, tag, n=50):
    """Load decisions matching a specific tag."""
    log_path = os.path.join(vault_root, DECISION_LOG_FILE)
    if not os.path.exists(log_path):
        return []
    entries = []
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line.strip())
                    if tag in entry.get("tags", []):
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    return entries[-n:]


def backfill_importance_scores(vault_root, entries_with_scores):
    """Rewrite the decision log with importance scores backfilled.
    entries_with_scores: list of {id: str, importance_score: int}.

    This reads the full log, updates matching entries, and rewrites.
    Only use at end-of-day/season — not per-cycle.
    """
    log_path = os.path.join(vault_root, DECISION_LOG_FILE)
    if not os.path.exists(log_path):
        return 0

    score_map = {e["id"]: e["importance_score"] for e in entries_with_scores}

    updated = 0
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception:
        return 0

    new_lines = []
    for line in lines:
        if not line.strip():
            new_lines.append(line)
            continue
        try:
            entry = json.loads(line.strip())
            eid = entry.get("id", "")
            if eid in score_map:
                entry["importance_score"] = score_map[eid]
                updated += 1
            new_lines.append(json.dumps(entry, ensure_ascii=False) + '\n')
        except json.JSONDecodeError:
            new_lines.append(line)

    if updated > 0:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

    return updated
