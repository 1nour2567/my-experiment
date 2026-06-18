"""
interrupt_system.py — Emergency interrupt detection for Agent Farm
===================================================================
Phase E6: Before the LLM makes its next decision, check a set of
configurable interrupt triggers. If any fire, PREPEND a high-priority
warning to the user_message — overriding normal decision priorities.

Design principles:
  1. Interrupts are DETERMINISTIC: same state → same interrupt or none.
  2. Triggers are data, not code: defined as a list of dicts with
     {name, condition(state, context), get_message(state, context)}.
  3. Interrupts modify context, not actions: they inject warnings into
     the prompt. The LLM still decides how to respond. The Constraint
     Architecture says "LLM reasons. Code decides what's allowed."
     — interrupts tell the LLM what the code considers URGENT.
  4. Multiple interrupts can fire simultaneously. All are shown.

Integration point:
    In agent-world-llm.py, AFTER building user_msg (sensory + observations
    + memory), BEFORE calling ask_llm_structured():
        interrupt_context = check_interrupts(state, context)
        if interrupt_context["active"]:
            user_msg = interrupt_context["header"] + user_msg
"""

# ═══════════════════════════ INTERRUPT TRIGGERS ═══════════════════════════
# Each trigger is a dict with:
#   name:       Unique identifier for logging
#   condition:  Lambda(state, context) → bool
#   message:    Lambda(state, context) → str  (the warning injected into prompt)
#   priority:   P0=life-critical, P1=high-risk, P2=advisory (for sorting)
#   cooldown_cycles: How many cycles before same interrupt can fire again

INTERRUPT_TRIGGERS = [
    # ── P0: LIFE-CRITICAL — agent/survival emergencies ──
    {
        "name": "energy_collapse",
        "condition": lambda s, ctx: (
            s.get("energy", 100) < 15
            and not s.get("is_night", False)
            and s.get("farmer", {}).get("sleepiness", 0) < 70
        ),
        "message": lambda s, ctx: _energy_collapse_msg(s),
        "priority": 0,
        "cooldown_cycles": 3,
    },
    {
        "name": "mature_crops_about_to_rot",
        "condition": lambda s, ctx: (
            len(_mature_crops(s)) >= 3
            and s.get("hour", 7) >= 16  # afternoon: running out of daylight
            and not s.get("is_night", False)
        ),
        "message": lambda s, ctx: _mature_rot_msg(s),
        "priority": 0,
        "cooldown_cycles": 2,
    },

    # ── P1: HIGH-RISK — crops/investment at immediate risk ──
    {
        "name": "frost_threat_to_near_harvest",
        "condition": lambda s, ctx: (
            s.get("frost_warning", False)
            and len(_crops_near_harvest(s)) >= 1
        ),
        "message": lambda s, ctx: _frost_threat_msg(s),
        "priority": 1,
        "cooldown_cycles": 5,
    },
    {
        "name": "storm_with_vulnerable_crops",
        "condition": lambda s, ctx: (
            s.get("weather", "") == "stormy"
            and not s.get("is_night", False)
            and len(s.get("crops", [])) > 0
        ),
        "message": lambda s, ctx: _storm_msg(s),
        "priority": 1,
        "cooldown_cycles": 4,
    },
    {
        "name": "repeated_failure_loop",
        "condition": lambda s, ctx: (
            ctx.get("last_failed", {}).get("count", 0) >= 2
        ),
        "message": lambda s, ctx: _failure_loop_msg(s, ctx),
        "priority": 1,
        "cooldown_cycles": 2,
    },

    # ── P2: ADVISORY — notable conditions worth flagging ──
    {
        "name": "starvation_warning",
        "condition": lambda s, ctx: (
            s.get("farmer", {}).get("hunger", 100) < 15
        ),
        "message": lambda s, ctx: _starvation_msg(s),
        "priority": 2,
        "cooldown_cycles": 5,
    },
    {
        "name": "drought_with_unwatered_crops",
        "condition": lambda s, ctx: (
            s.get("weather", "") == "drought"
            and not s.get("is_night", False)
            and len(_unwatered_crops(s)) >= 3
        ),
        "message": lambda s, ctx: _drought_msg(s),
        "priority": 2,
        "cooldown_cycles": 3,
    },
    {
        "name": "sick_animal_untreated",
        "condition": lambda s, ctx: (
            any(a.get("sick") for a in s.get("livestock", []))
        ),
        "message": lambda s, ctx: _sick_animal_msg(s),
        "priority": 2,
        "cooldown_cycles": 8,
    },
    {
        "name": "storage_overflow_risk",
        "condition": lambda s, ctx: (
            len(s.get("storage", [])) >= s.get("storage_capacity", 50) * 0.9
            and len(_mature_crops(s)) >= 1
        ),
        "message": lambda s, ctx: _storage_full_msg(s),
        "priority": 2,
        "cooldown_cycles": 5,
    },
    {
        "name": "extreme_sleepiness",
        "condition": lambda s, ctx: (
            s.get("farmer", {}).get("sleepiness", 0) >= 70
            and not s.get("is_night", False)
        ),
        "message": lambda s, ctx: _sleepiness_msg(s),
        "priority": 2,
        "cooldown_cycles": 4,
    },
]


# ═══════════════════════════ CORE API ═══════════════════════════

# Per-trigger cooldown tracking (reset each session / each game day)
_cooldowns = {}  # {trigger_name: last_cycle_fired}


def check_interrupts(state, context=None):
    """Check all interrupt triggers against current farm state.

    Args:
        state:   Full farm state dict (from API).
        context: Optional dict with extra runtime info:
                 - last_failed: {action, count} for repeated failure tracking
                 - cycle: current cycle number (for cooldown tracking)

    Returns:
        Dict with:
          active:   bool — True if any interrupts fired.
          fired:    list of {name, priority, message} — triggered interrupts.
          header:   str — ready-to-prepend markdown header ('' if none active).
    """
    if context is None:
        context = {}

    fired = []
    for trigger in INTERRUPT_TRIGGERS:
        # Check cooldown
        name = trigger["name"]
        cooldown = trigger.get("cooldown_cycles", 0)
        if name in _cooldowns and (context.get("cycle", 0) - _cooldowns[name]) < cooldown:
            continue

        # Check condition
        try:
            if trigger["condition"](state, context):
                msg = trigger["message"](state, context)
                fired.append({
                    "name": name,
                    "priority": trigger["priority"],
                    "message": msg,
                })
                _cooldowns[name] = context.get("cycle", 0)
        except Exception:
            pass  # trigger malfunction shouldn't crash the agent

    if not fired:
        return {"active": False, "fired": [], "header": ""}

    # Sort by priority (P0 first)
    fired.sort(key=lambda t: t["priority"])

    # Build the interrupt header
    header = _build_header(fired)

    return {
        "active": True,
        "fired": fired,
        "header": header,
    }


def reset_cooldowns():
    """Reset all trigger cooldowns (call at start of each new day)."""
    _cooldowns.clear()


# ═══════════════════════════ MESSAGE BUILDERS ═══════════════════════════

def _build_header(fired):
    """Build the interrupt header block to prepend to user_msg."""
    lines = []
    lines.append("## 🚨 紧急中断 — 以下情况要求立即处理")
    lines.append("")

    for t in fired:
        icon = "🔴" if t["priority"] == 0 else "🟠" if t["priority"] == 1 else "🟡"
        lines.append(f"{icon} **{t['message']}**")
    lines.append("")
    lines.append("> ⚡ 以下所有正常观察仍然有效，但上述紧急情况必须优先考虑。")

    return "\n".join(lines) + "\n\n"


def _mature_crops(state):
    """Return list of mature crops (gdd >= 95%)."""
    return [c for c in state.get("crops", []) if c.get("gdd_percent", 0) >= 95]


def _crops_near_harvest(state):
    """Return list of crops near harvest (gdd >= 50%)."""
    return [c for c in state.get("crops", []) if c.get("gdd_percent", 0) >= 50]


def _unwatered_crops(state):
    """Return list of crops that haven't been watered today."""
    return [c for c in state.get("crops", []) if not c.get("watered_today", False)]


# ── Individual message generators ──

def _energy_collapse_msg(state):
    e = state.get("energy", 0)
    hour = state.get("hour", 7)
    return (
        f"体力仅剩{e}（濒临昏迷阈值）——不要让农夫继续劳作。"
        f"立即 sleep 或至少 eat+drink 恢复体力。现在{hour:.0f}点，天黑前还有时间恢复。"
    )


def _mature_rot_msg(state):
    mature = _mature_crops(state)
    names = ", ".join(
        f"{c.get('crop_name',c.get('crop_type','?'))}({c.get('gdd_percent',0)}%)"
        for c in mature[:5]
    )
    hour = state.get("hour", 7)
    return (
        f"{len(mature)}株作物已完全成熟({names})，现在{hour:.0f}点天黑前必须 harvest！"
        f"再过夜可能腐烂贬值。丢弃其他任务，立即收获。"
    )


def _frost_threat_msg(state):
    near = _crops_near_harvest(state)
    names = ", ".join(
        f"{c.get('crop_name',c.get('crop_type','?'))}({c.get('gdd_percent',0)}%)"
        for c in near[:3]
    )
    return (
        f"霜冻预警！{len(near)}株接近成熟的作物({names})面临冻伤风险。"
        f"如果gdd>=95，立即 harvest；否则考虑用温室或覆盖物保护。"
    )


def _storm_msg(state):
    crops = len(state.get("crops", []))
    return (
        f"暴风雨袭来！{crops}株作物面临积水、土壤侵蚀和倒伏风险。"
        f"确保排水沟畅通(drain)，保护牲畜(bring_to_shelter)，避免在户外做低优先级工作。"
    )


def _failure_loop_msg(state, ctx):
    lf = ctx.get("last_failed", {})
    action = lf.get("action", "?")
    count = lf.get("count", 0)
    return (
        f"'`{action}`'操作已连续失败{count}次——陷入了重复失败循环。"
        f"停止尝试`{action}`。检查失败原因（lookup），或改为执行不同动作。"
    )


def _starvation_msg(state):
    hunger = state.get("farmer", {}).get("hunger", 0)
    return (
        f"饥饿值仅剩{hunger}——接近饿死阈值(<5)。"
        f"立即 eat。如果没食物，去市场 buy food。"
    )


def _drought_msg(state):
    unwatered = _unwatered_crops(state)
    return (
        f"旱灾中，{len(unwatered)}株作物未浇水。旱灾期间不浇水=当天枯萎。"
        f"最高优先级：water 所有作物。"
    )


def _sick_animal_msg(state):
    sick = [a.get("name", "?") for a in state.get("livestock", []) if a.get("sick")]
    return (
        f"{'、'.join(sick)} 生病了！不治疗可能死亡。"
        f"尽快 treat_animal。如果不会治疗，至少 isolate 隔离防止传染。"
    )


def _storage_full_msg(state):
    used = len(state.get("storage", []))
    cap = state.get("storage_capacity", 50)
    return (
        f"仓库已满{used}/{cap}（90%+），且还有成熟作物待收。"
        f"先 sell_storage 清出空间，再 harvest——否则收获后无处存放。"
    )


def _sleepiness_msg(state):
    sl = state.get("farmer", {}).get("sleepiness", 0)
    return (
        f"睡意已达{sl}/80。≥80将强制只能 sleep，无法做任何农活。"
        f"考虑在睡意触发强制sleep前完成关键任务，或提前sleep恢复。"
    )


# ═══════════════════════════ SELF-TEST ═══════════════════════════

if __name__ == "__main__":
    # Test each trigger with mock state
    tests = [
        ("energy_collapse", {"energy": 10, "is_night": False, "hour": 14, "farmer": {"sleepiness": 40}}),
        ("mature_crops_about_to_rot", {"crops": [
            {"crop_name": "wheat", "gdd_percent": 97},
            {"crop_name": "wheat", "gdd_percent": 98},
            {"crop_name": "wheat", "gdd_percent": 96},
        ], "hour": 17, "is_night": False}),
        ("frost_threat_to_near_harvest", {"frost_warning": True, "crops": [
            {"crop_name": "pumpkin", "gdd_percent": 85},
        ]}),
        ("storm_with_vulnerable_crops", {"weather": "stormy", "is_night": False, "crops": [
            {"crop_name": "tomato", "gdd_percent": 60},
        ]}),
        ("repeated_failure_loop", {"crops": []}, {"last_failed": {"action": "drain", "count": 3}}),
        ("no_interrupt", {"energy": 100, "is_night": False, "hour": 10, "crops": [
            {"crop_name": "wheat", "gdd_percent": 30, "watered_today": True},
        ], "farmer": {"hunger": 80, "sleepiness": 20}, "weather": "sunny"}),
    ]

    for label, state, *ctx in tests:
        context = ctx[0] if ctx else {}
        context.setdefault("cycle", 1)
        result = check_interrupts(state, context)
        status = f"{len(result['fired'])} fired" if result["active"] else "inactive"
        expected_trigger = "no_interrupt" if label == "no_interrupt" else label
        if result["active"]:
            got = result["fired"][0]["name"]
            match = "MATCH" if got == expected_trigger else f"MISMATCH (expected {expected_trigger})"
            print(f"  [{match}] {label} -> {status}: {got} p={result['fired'][0]['priority']}")
        else:
            print(f"  [{'OK' if label == 'no_interrupt' else 'MISS'}] {label} -> {status}")
