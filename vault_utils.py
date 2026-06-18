"""
vault_utils.py — Shared vault I/O + Phase 0 decision log layer
===============================================================
Used by agent-world-llm.py and agent-world-brain.py.
Provides normalized cross-platform vault read/write and
Phase 0 JSONL decision logging infrastructure.
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


# ═══════════════════════════ Phase 0: Decision Log Layer ═══════════════════════════

DECISION_LOG_FILE = "decisions/decision_log.jsonl"


def log_decision(vault_root, decision_id, state, action, params,
                 success, result_msg, gcg_metrics=None):
    """Append one structured decision entry to the JSONL log.

    Called after every agent execution cycle. Coexists with the
    human-readable Markdown decision files — this is for machine retrieval.
    """
    entry = {
        "id": decision_id,
        "timestamp": datetime.datetime.now().isoformat(),
        "season": state.get("season", "?"),
        "day": state.get("day", 0),
        "year": state.get("year", 1),
        "action": action,
        "params": params,
        "state_snippet": (
            f"gold={state.get('gold',0)} energy={state.get('energy',0)} "
            f"tilled={state.get('tilled',0)} planted={state.get('planted',0)} "
            f"weather={state.get('weather','?')}"
        ),
        "success": success,
        "result": str(result_msg)[:200],
        "error": None if success else str(result_msg)[:200],
        "gcg": gcg_metrics or {},
    }
    full = os.path.join(vault_root, DECISION_LOG_FILE)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


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
