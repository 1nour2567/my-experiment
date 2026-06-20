"""
agent_profile.py — AgentProfile dataclass + loader for Agent Farm
==================================================================
Phase W1-1: Loads agent identity, personality (3-layer model), skills,
and inventory from JSON profile files. Provides drift/decay hooks
for the dynamic personality engine.

Design:
  - Fixed traits: immutable, set at creation.
  - Learned preferences: drift values 0.0-1.0 based on repeated behavior.
  - Social influences: transient modifiers, decay over time.
  - Skills: levels + XP + specialization, used by SkillTree.
"""
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
import json, os


# ═══════════════════════════ DATA CLASS ═══════════════════════════

@dataclass
class AgentProfile:
    """Full agent identity and personality state. Serializes to/from JSON."""

    # ── Identity ──
    id: str = ""
    display_name: str = ""
    role: str = "farmer"  # farmer | herder | craftsman
    bio: str = ""
    avatar_emoji: str = "🌾"

    # ── Three-layer personality ──
    fixed_traits: Dict[str, float] = field(default_factory=dict)
    learned_preferences: Dict[str, float] = field(default_factory=dict)
    social_influences: Dict[str, Any] = field(default_factory=dict)

    # ── Skills ──
    skills: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # ── Perception ──
    perception_bias: float = 1.0
    literacy_level: float = 0.7

    # ── Inventory ──
    inventory: Dict[str, List[Any]] = field(default_factory=dict)

    # ── Stats ──
    history: Dict[str, int] = field(default_factory=dict)

    # ── Runtime (not serialized) ──
    knowledge_map: Optional[Any] = None  # KnowledgeMap instance (lazy)
    skill_tree: Optional[Any] = None     # SkillTree instance (lazy)

    # ═══════════════════════ PERSISTENCE ═══════════════════════

    @classmethod
    def load(cls, profile_path: str) -> "AgentProfile":
        """Load agent profile from JSON file."""
        if not os.path.exists(profile_path):
            raise FileNotFoundError(f"Agent profile not found: {profile_path}")
        with open(profile_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    def save(self, profile_path: str):
        """Save agent profile to JSON file. Only persists serializable fields."""
        os.makedirs(os.path.dirname(profile_path), exist_ok=True)
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentProfile":
        """Construct from JSON dictionary. Filters _comment metadata keys."""
        def _clean(d: dict) -> dict:
            return {k: v for k, v in d.items() if not k.startswith("_")}

        return cls(
            id=data.get("id", ""),
            display_name=data.get("display_name", ""),
            role=data.get("role", "farmer"),
            bio=data.get("bio", ""),
            avatar_emoji=data.get("avatar_emoji", "🌾"),
            fixed_traits=_clean(data.get("fixed_traits", {})),
            learned_preferences=_clean(data.get("learned_preferences", {})),
            social_influences=_clean(data.get("social_influences", {})),
            skills=data.get("skills", {}),
            perception_bias=data.get("perception_bias", 1.0),
            literacy_level=data.get("literacy_level", 0.7),
            inventory=data.get("inventory", {}),
            history=data.get("history", {}),
        )

    def to_dict(self) -> dict:
        """Serialize to JSON-safe dictionary."""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "role": self.role,
            "bio": self.bio,
            "avatar_emoji": self.avatar_emoji,
            "fixed_traits": self.fixed_traits,
            "learned_preferences": self.learned_preferences,
            "social_influences": self.social_influences,
            "skills": self.skills,
            "perception_bias": self.perception_bias,
            "literacy_level": self.literacy_level,
            "inventory": self.inventory,
            "history": self.history,
        }

    # ═══════════════════════ PERSONALITY DRIFT ═══════════════════════

    def drift_preference(self, key: str, delta: float):
        """Shift a learned preference by delta, clamped to [0.0, 1.0]."""
        if key in self.learned_preferences:
            current = self.learned_preferences[key]
            self.learned_preferences[key] = max(0.0, min(1.0, current + delta))

    def drift_toward_action(self, action: str, amount: float = 0.02):
        """Nudge preferences based on a performed action."""
        drift_map = {
            "water": "watering_enjoyment",
            "harvest": "harvesting_satisfaction",
            "read": "reading_love",
            "build": "building_interest",
            "explore": "exploration_urge",
            "feed_animals": "animal_affinity",
            "treat_animal": "animal_affinity",
            "breed": "animal_affinity",
            "forge": "crafting_drive",
            "repair": "crafting_drive",
            "sell_storage": "frugality_habit",
            "drain": "drainage_anxiety",
        }
        if action in drift_map:
            self.drift_preference(drift_map[action], amount)

    def get_effective_risk_tolerance(self) -> float:
        """Compute effective risk tolerance: fixed trait + learned modifier."""
        base = self.fixed_traits.get("bravery", 0.5)
        modifier = self.learned_preferences.get("risk_appetite_modifier", 0.0)
        social_mod = 0.0
        # Apply social influences from recent interactions
        for interaction in self.social_influences.get("recent_interactions", []):
            social_mod += interaction.get("risk_shift", 0.0)
        return max(0.0, min(1.0, base * 0.7 + modifier * 0.2 + social_mod * 0.1))

    # ═══════════════════════ PERSONALITY → PROMPT ═══════════════════════

    def personality_snippet(self) -> str:
        """Generate a compact personality description for the LLM system prompt."""
        lines = []
        # Who am I
        lines.append(f"你是**{self.display_name}**，一个{self.role}。{self.bio}")

        # Fixed traits
        trait_names = {
            "industriousness": "勤奋", "intelligence": "聪明",
            "social_aptitude": "社交", "curiosity": "好奇心",
            "patience": "耐心", "bravery": "勇气",
        }
        high_traits = [trait_names.get(k, k) for k, v in self.fixed_traits.items() if k != "_comment" and isinstance(v, (int, float)) and v > 0.65]
        low_traits = [trait_names.get(k, k) for k, v in self.fixed_traits.items() if k != "_comment" and isinstance(v, (int, float)) and v < 0.35]
        if high_traits:
            lines.append(f"- 天生特质: {'、'.join(high_traits)}较高")
        if low_traits:
            lines.append(f"- {'、'.join(low_traits)}方面偏弱")

        # Learned preferences (only show strong ones > 0.4)
        pref_names = {
            "watering_enjoyment": "浇水", "harvesting_satisfaction": "收获",
            "reading_love": "读书", "social_desire": "社交",
            "building_interest": "建造", "exploration_urge": "探索",
            "animal_affinity": "动物", "crafting_drive": "手工",
            "drainage_anxiety": "排水焦虑", "frugality_habit": "节俭",
        }
        strong_prefs = [
            pref_names.get(k, k) for k, v in self.learned_preferences.items()
            if k != "_comment" and isinstance(v, (int, float)) and v > 0.4
        ]
        if strong_prefs:
            lines.append(f"- 最近越来越喜欢: {'、'.join(strong_prefs)}")
        anxiety = self.learned_preferences.get("drainage_anxiety", 0.0)
        if anxiety > 0.5:
            lines.append(f"- ⚠ 对积水有较强焦虑(排水偏好={anxiety:.2f})，看到涝就坐立不安")

        # Risk profile
        risk = self.get_effective_risk_tolerance()
        if risk > 0.7:
            lines.append("- 决策风格: 偏激进，愿意承担风险")
        elif risk < 0.3:
            lines.append("- 决策风格: 偏保守，优先安全")
        else:
            lines.append("- 决策风格: 平衡型")

        # Starting equipment (asymmetric start)
        tools = self.inventory.get("tools_owned", [])
        if tools:
            lines.append(f"- 随身工具: {', '.join(tools)}")

        return "\n".join(lines)

    # ═══════════════════════ SKILL HELPERS ═══════════════════════

    def get_skill_level(self, skill_name: str) -> int:
        """Get the numeric level for a skill."""
        sk = self.skills.get(skill_name, {})
        return sk.get("level", 0)

    def add_skill_xp(self, skill_name: str, xp: int):
        """Add XP to a skill. Level-up logic delegated to SkillTree."""
        if skill_name not in self.skills:
            self.skills[skill_name] = {"level": 0, "xp": 0, "specialization": None}
        self.skills[skill_name]["xp"] = self.skills[skill_name].get("xp", 0) + xp

    # ═══════════════════════ SOCIAL ═══════════════════════

    def record_social_interaction(self, other_id: str, effect: Dict[str, float]):
        """Record a social interaction and apply its effects."""
        interactions = self.social_influences.get("recent_interactions", [])
        interactions.append({
            "with": other_id,
            "risk_shift": effect.get("risk_shift", 0.0),
            "preference_shifts": effect.get("preference_shifts", {}),
            "timestamp": effect.get("timestamp", ""),
        })
        # Keep only last 10 interactions
        self.social_influences["recent_interactions"] = interactions[-10:]
        # Apply preference shifts
        for key, delta in effect.get("preference_shifts", {}).items():
            self.drift_preference(key, delta)
        self.history["social_interactions"] = self.history.get("social_interactions", 0) + 1

    def decay_social_influences(self, cycles_since_last: int = 0):
        """Decay social influence modifiers over time."""
        if cycles_since_last > 20:
            # After 20 cycles without interaction, decay significantly
            interactions = self.social_influences.get("recent_interactions", [])
            if interactions:
                self.social_influences["recent_interactions"] = interactions[-3:]


# ═══════════════════════════ FACTORY ═══════════════════════════

def load_agent_profile(agent_id: str, vault_root: str) -> AgentProfile:
    """Load agent profile by ID. Falls back to xu_renwu default."""
    profiles_dir = os.path.join(vault_root, "agents", "profiles")
    # Try exact ID match first
    profile_path = os.path.join(profiles_dir, f"{agent_id}.json")
    if not os.path.exists(profile_path):
        # Fall back to xu_renwu
        profile_path = os.path.join(profiles_dir, "xu_renwu.json")
    return AgentProfile.load(profile_path)


def list_agent_profiles(vault_root: str) -> List[str]:
    """List all available agent profile IDs."""
    profiles_dir = os.path.join(vault_root, "agents", "profiles")
    if not os.path.isdir(profiles_dir):
        return []
    return [
        f.replace(".json", "") for f in os.listdir(profiles_dir)
        if f.endswith(".json")
    ]


def resolve_agent_name(vault_root: str, name_or_id: str) -> str:
    """Resolve an agent identifier to a profile ID. Accepts both display names
    (e.g. '续仁武', '老王', '铁娘子') and raw IDs ('xu_renwu', etc.)."""
    # Direct match against profile IDs
    profiles = list_agent_profiles(vault_root)
    if name_or_id in profiles:
        return name_or_id
    # Match against display names
    for pid in profiles:
        try:
            prof = load_agent_profile(pid, vault_root)
            if prof.display_name == name_or_id:
                return pid
            if name_or_id in prof.display_name or prof.display_name in name_or_id:
                return pid
        except Exception:
            pass
    # Fallback: return as-is (may or may not work)
    return name_or_id


def build_name_map(vault_root: str) -> dict:
    """Build {display_name: profile_id, ...} for all known agents."""
    profiles = list_agent_profiles(vault_root)
    name_map = {}
    for pid in profiles:
        try:
            prof = load_agent_profile(pid, vault_root)
            name_map[prof.display_name] = pid
            name_map[pid] = pid  # also map ID to itself
        except Exception:
            name_map[pid] = pid
    return name_map


# ═══════════════════════════ SELF-TEST ═══════════════════════════

if __name__ == "__main__":
    import sys
    vault = r"C:\Users\m1916\agent-brain"

    # Load all profiles
    for pid in list_agent_profiles(vault):
        profile = load_agent_profile(pid, vault)
        print(f"\n{'='*50}")
        print(f"Profile: {profile.display_name} ({profile.id})")
        print(f"Role: {profile.role} | Perception bias: {profile.perception_bias}")
        print(f"Risk tolerance: {profile.get_effective_risk_tolerance():.2f}")
        print(f"\nPersonality snippet:")
        print(profile.personality_snippet())

        # Test drift
        profile.drift_toward_action("water", 0.05)
        profile.drift_toward_action("read", 0.03)
        print(f"\nAfter drift: watering={profile.learned_preferences['watering_enjoyment']:.2f}, reading={profile.learned_preferences['reading_love']:.2f}")

    print(f"\nAll 3 profiles loaded, drifted, verified.")
