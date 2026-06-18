"""
importance_scorer.py — Importance scoring engine for Agent Farm events
======================================================================
Phase E5: Assigns 0-10 importance scores to FarmEvents based on
configurable rule set. Runs the memory synthesizer at end-of-day
to separate signal from noise.

Design principles:
  1. Rules are data, not code (importable JSON scoring spec).
  2. Real-time scoring hints (tags, heuristics) + deferred scoring (season context).
  3. Low-importance events discarded from long-term memory; high ones kept.
  4. Reflection generator: extracts patterns from high-importance events.
"""
import json
import os
import re as _re

# ═══════════════════════════ SCORING RULES ═══════════════════════════
# Each rule: {name, condition, score, reason}
# condition is a lambda-style check against the entry dict.
# When condition returns True, score is added and reason recorded.

SCORING_RULES = [
    # ── Harvest quality ──
    {
        "name": "perfect_harvest_timing",
        "score": 3,
        "reason": "在最佳时机收割——作物完全成熟",
        "check": lambda e: (
            e.get("action") == "harvest" and e.get("success")
        ),
    },
    {
        "name": "high_value_harvest",
        "score": 2,
        "reason": "收获价值超过500G——重大收入",
        "check": lambda e: (
            e.get("action") == "harvest" and e.get("success")
            and e.get("outcome", {}).get("estimated_value", 0) > 500
        ),
    },
    {
        "name": "high_quality_harvest",
        "score": 2,
        "reason": "收获了S级或A级高品质作物",
        "check": lambda e: (
            e.get("action") == "harvest" and e.get("success")
            and e.get("outcome", {}).get("quality", "B") in ("S", "A")
        ),
    },

    # ── Economy ──
    {
        "name": "large_sale",
        "score": 2,
        "reason": "单次出售收入超过1000G",
        "check": lambda e: (
            e.get("action") == "sell_storage" and e.get("success")
            and e.get("outcome", {}).get("revenue", 0) > 1000
        ),
    },
    {
        "name": "first_major_purchase",
        "score": 2,
        "reason": "首次购买——扩展农场能力",
        "check": lambda e: (
            e.get("action") in ("buy", "buy_tool", "buy_animal", "buy_material")
            and e.get("success")
        ),
    },

    # ── Construction ──
    {
        "name": "new_building",
        "score": 3,
        "reason": "建造了新建筑——永久性农场升级",
        "check": lambda e: (
            e.get("action") in ("build", "propose_building") and e.get("success")
        ),
    },

    # ── Learning from failure ──
    {
        "name": "costly_failure",
        "score": 2,
        "reason": "失败中学习——避免未来损失",
        "check": lambda e: (
            not e.get("success")
            and e.get("action") not in ("next_day", "lookup", "water", "sleep", "eat", "drink_water", "recall")
        ),
    },

    # ── Livestock ──
    {
        "name": "animal_health_intervention",
        "score": 2,
        "reason": "治疗生病动物——防止死亡损失",
        "check": lambda e: (
            e.get("action") == "treat_animal" and e.get("success")
        ),
    },
    {
        "name": "first_breeding",
        "score": 2,
        "reason": "首次繁殖——扩大畜群",
        "check": lambda e: (
            e.get("action") == "breed" and e.get("success")
        ),
    },

    # ── Tool upgrades ──
    {
        "name": "tool_upgrade",
        "score": 2,
        "reason": "升级工具——永久性效率提升",
        "check": lambda e: (
            e.get("action") in ("forge", "buy_tool") and e.get("success")
        ),
    },

    # ── Extreme weather survival ──
    {
        "name": "storm_survival",
        "score": 2,
        "reason": "极端天气下采取了正确的保护措施",
        "check": lambda e: (
            e.get("action") == "drain" and e.get("success")
            and "stormy" in str(e.get("context_before", {}).get("weather", ""))
        ),
    },

    # ── Routine (base score, may be overridden) ──
    {
        "name": "routine_maintenance",
        "score": 1,
        "reason": "日常维护——保持农场运转",
        "check": lambda e: (
            e.get("success")
            and e.get("action") in ("water", "till", "weed_all", "feed_animals", "eat", "drink_water")
        ),
    },
    {
        "name": "meta_action",
        "score": 0,
        "reason": "元操作——无直接的农场状态变化",
        "check": lambda e: (
            e.get("action") in ("next_day", "lookup", "recall", "remember", "forget", "sleep")
        ),
    },
]


class ImportanceScorer:
    """Score FarmEvent entries using configurable rules.

    Usage:
        scorer = ImportanceScorer()
        scores = scorer.score_entries(entries)  # [(entry_id, score, reasons), ...]
        high = scorer.filter_important(entries, min_score=5)
    """

    def __init__(self, rules=None):
        self.rules = rules or SCORING_RULES

    def score_entry(self, entry):
        """Score a single entry. Returns (score, [reasons])."""
        total = 0
        reasons = []
        for rule in self.rules:
            try:
                if rule["check"](entry):
                    total += rule["score"]
                    reasons.append(rule["reason"])
            except Exception:
                pass  # rule check failed silently
        return min(total, 10), reasons  # cap at 10

    def score_entries(self, entries):
        """Score multiple entries. Returns [{id, score, reasons}, ...]."""
        results = []
        for e in entries:
            score, reasons = self.score_entry(e)
            results.append({
                "id": e.get("id", ""),
                "importance_score": score,
                "reasons": reasons,
            })
        return results

    def filter_important(self, entries, min_score=5):
        """Return only entries with importance >= min_score."""
        return [e for e in entries if e.get("importance_score", 0) >= min_score]

    def get_rule_stats(self):
        """Return summary of scoring rules."""
        return {
            "total_rules": len(self.rules),
            "max_possible_score": sum(r["score"] for r in self.rules),
            "rules": [{"name": r["name"], "score": r["score"]} for r in self.rules],
        }


# ═══════════════════════════ MEMORY SYNTHESIZER ═══════════════════════════

class MemorySynthesizer:
    """End-of-day memory synthesis: score, filter, reflect.

    Runs when next_day is called. It:
      1. Loads today's (or this season's) decision log entries.
      2. Scores each entry with ImportanceScorer.
      3. Generates a daily summary from high-importance events.
      4. At season end, generates a reflection narrative.

    The reflection is a natural-language summary of what was learned,
    tagged as event_type: reflection, and stored as a high-importance
    memory entry for future retrieval.
    """

    def __init__(self, vault_root, scorer=None):
        self.vault_root = vault_root
        self.scorer = scorer or ImportanceScorer()

    def synthesize_day(self, season, year, day):
        """Score and summarize one day's events.

        Returns:
            dict with {scored_count, high_importance_count, summary}
        """
        # Import here to avoid circular dependency
        import vault_utils as vu

        entries = vu.load_decisions_for_day_range(
            self.vault_root, season, year, day, day
        )
        if not entries:
            return {"scored_count": 0, "high_importance_count": 0, "summary": ""}

        scored = self.scorer.score_entries(entries)

        # Backfill scores into the log
        vu.backfill_importance_scores(self.vault_root, scored)

        high = [s for s in scored if s["importance_score"] >= 5]
        med = [s for s in scored if 3 <= s["importance_score"] < 5]

        summary_lines = []
        if high:
            summary_lines.append(f"## ⭐ 高重要性 ({len(high)}件)")
            for s in high[:5]:
                summary_lines.append(f"- [{s['importance_score']}] {'; '.join(s['reasons'][:2])}")

        return {
            "scored_count": len(scored),
            "high_importance_count": len(high),
            "medium_count": len(med),
            "summary": "\n".join(summary_lines) if summary_lines else "(日常维护，无特殊事件)",
        }

    def generate_reflection(self, season, year):
        """Generate a season-end reflection from high-importance events.

        This is a deterministic text generation — not an LLM call.
        The output is stored as a FarmEvent with event_type: reflection.
        """
        import vault_utils as vu

        entries = vu.load_decisions_for_season(self.vault_root, season, year)
        if not entries or len(entries) < 10:
            return None

        # Score all entries
        scored = self.scorer.score_entries(entries)

        # Aggregate statistics
        total = len(scored)
        high = [s for s in scored if s["importance_score"] >= 5]
        routine = [s for s in scored if s["importance_score"] <= 1]

        # Action breakdown
        action_counts = {}
        for e in entries:
            act = e.get("action", "?")
            action_counts[act] = action_counts.get(act, 0) + 1

        top_actions = sorted(action_counts.items(), key=lambda x: -x[1])[:5]
        success_count = sum(1 for e in entries if e.get("success"))
        fail_count = total - success_count

        # Find most common reason for high scores
        reason_counts = {}
        for s in high:
            for r in s.get("reasons", []):
                reason_counts[r] = reason_counts.get(r, 0) + 1
        top_reasons = sorted(reason_counts.items(), key=lambda x: -x[1])[:3]

        # Build reflection text
        reflection = f"# 📊 {season} Y{year} 记忆合成\n\n"
        reflection += f"## 统计\n"
        reflection += f"- 总事件: {total} | 成功 {success_count}/{fail_count} 失败\n"
        reflection += f"- ⭐ 重要事件: {len(high)} ({100*len(high)//max(1,total)}%)\n"
        reflection += f"- 📋 日常事件: {len(routine)} ({100*len(routine)//max(1,total)}%)\n\n"

        reflection += f"## 主要活动\n"
        for act, cnt in top_actions:
            reflection += f"- {act}: {cnt}次\n"

        if top_reasons:
            reflection += f"\n## 值得记住的事\n"
            for reason, cnt in top_reasons:
                reflection += f"- {reason} ({cnt}次)\n"

        # Season-level insights (rule-based, not LLM)
        insights = self._generate_insights(entries, season, year)
        if insights:
            reflection += f"\n## 💡 本季发现\n"
            for insight in insights:
                reflection += f"- {insight}\n"

        reflection += f"\n> 自动生成 by MemorySynthesizer | {len(high)}件重要事件已存入长期记忆\n"

        return reflection

    def _generate_insights(self, entries, season, year):
        """Generate rule-based insights from a season's entries."""
        insights = []

        # Check for weather-related patterns
        storm_drains = sum(
            1 for e in entries
            if e.get("action") == "drain" and e.get("success")
            and "stormy" in str(e.get("tags", []))
        )
        if storm_drains >= 3:
            insights.append(f"本季遭遇了至少{storm_drains}次暴风雨积水——考虑在低洼地预先挖排水沟")

        # Check harvest quality
        harvests = [e for e in entries if e.get("action") == "harvest" and e.get("success")]
        if harvests:
            high_q = sum(
                1 for e in harvests
                if e.get("outcome", {}).get("quality", "B") in ("S", "A")
            )
            if high_q > 0:
                insights.append(f"收获了{high_q}次高品质作物——说明当前土壤和水肥管理有效")
            elif len(harvests) >= 10:
                insights.append("本季收获品质以B/C为主——考虑增加施肥(compost/fertilize)提升品质")

        # Check for repeated failures
        fails = [e for e in entries if not e.get("success")]
        failure_actions = {}
        for e in fails:
            act = e.get("action", "?")
            failure_actions[act] = failure_actions.get(act, 0) + 1
        repeat_fails = [(act, cnt) for act, cnt in failure_actions.items() if cnt >= 3]
        if repeat_fails:
            worst = max(repeat_fails, key=lambda x: x[1])
            insights.append(f"`{worst[0]}`操作失败了{worst[1]}次——需要找出原因或避免此动作")

        # Check for economic growth
        gold_values = []
        for e in entries:
            ctx = e.get("context_before", {})
            if "gold" in ctx:
                gold_values.append(ctx["gold"])
        if len(gold_values) >= 5:
            start_gold = gold_values[0]
            end_gold = gold_values[-1]
            if end_gold > start_gold * 1.5:
                insights.append(f"金币从{start_gold}G增长到{end_gold}G——经济增长良好")
            elif end_gold < start_gold * 0.8:
                insights.append(f"金币从{start_gold}G下降到{end_gold}G——需要调整经济策略")

        return insights


# ═══════════════════════════ QUICK TEST ═══════════════════════════

if __name__ == "__main__":
    # Quick self-test
    scorer = ImportanceScorer()

    test_entries = [
        {"id": "t1", "action": "harvest", "success": True,
         "outcome": {"estimated_value": 800, "quality": "A"}},
        {"id": "t2", "action": "water", "success": True, "outcome": {}},
        {"id": "t3", "action": "build", "success": True, "outcome": {"building": "fence"}},
        {"id": "t4", "action": "plant", "success": False, "outcome": {},
         "context_before": {"weather": "stormy"}},
        {"id": "t5", "action": "next_day", "success": True, "outcome": {}},
    ]

    results = scorer.score_entries(test_entries)
    for r in results:
        print(f"  {r['id']}: score={r['importance_score']} {r['reasons'][:2]}")

    assert results[0]["importance_score"] >= 5, "harvest should be important"
    assert results[1]["importance_score"] <= 2, "water should be routine"
    assert results[2]["importance_score"] >= 3, "build should be important"
    assert results[3]["importance_score"] >= 2, "failure should be noted"
    assert results[4]["importance_score"] == 0, "next_day should be zero"
    print("All self-tests passed!")
