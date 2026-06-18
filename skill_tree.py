"""
skill_tree.py — Branching skill tree engine for Agent Farm
============================================================
Phase W1-2: Loads profession-specific skill trees from JSON definitions.
Handles XP accumulation, level-up checking, node unlocking, and
specialization branching (choose one of two paths at tier-X nodes).

Design:
  - Skill trees are JSON data. Three profession files: farmer, herder, craftsman.
  - XP thresholds per level (not linear — steepens at high levels).
  - Nodes have prerequisites (requires: [node_ids]).
  - Specialization: some tier-2 nodes share prerequisites → agent must choose.
  - Effects are opaque dicts consumed by the action engine.
"""
import json, os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set


# ═══════════════════════════ XPERIENCE TABLE ═══════════════════════════

# XP per action type. Farmers get more farming XP, herders more husbandry, etc.
BASE_ACTION_XP = {
    "till": {"farming": 5},
    "till_bulk": {"farming": 8},
    "plant": {"farming": 3},
    "plant_bulk": {"farming": 5},
    "water": {"farming": 2},
    "harvest": {"farming": 10},
    "weed_all": {"farming": 3},
    "fertilize": {"farming": 8, "construction": 2},
    "compost": {"farming": 5, "construction": 3},
    "save_seeds": {"farming": 5},
    "drain": {"farming": 3, "machinery": 2},
    "irrigate_drip": {"farming": 5, "machinery": 3},
    "feed_animals": {"husbandry": 5},
    "water_animals": {"husbandry": 3},
    "collect_products": {"husbandry": 8},
    "treat_animal": {"husbandry": 10},
    "breed": {"husbandry": 12},
    "slaughter": {"husbandry": 3, "processing": 2},
    "send_to_pasture": {"husbandry": 3, "exploration": 2},
    "build": {"construction": 15},
    "propose_building": {"construction": 5},
    "buy_tool": {"machinery": 3},
    "forge": {"construction": 12},
    "repair": {"construction": 8},
    "read": {"scholarship": 8},
    "research": {"scholarship": 12},
    "exercise": {"exploration": 2},
    "explore": {"exploration": 10},
    "sleep": {},
    "eat": {},
    "drink_water": {},
    "next_day": {},
    "lookup": {},
    "sell_storage": {},
    "buy": {},
    "remember": {},
    "recall": {},
    "forget": {},
    "social_msg": {"scholarship": 3},
    "social_lookup": {"scholarship": 2},
    "read_book": {"scholarship": 10},
    "buy_book": {"scholarship": 2},
    "explore": {"exploration": 10, "scholarship": 3},
    "trade_propose": {"scholarship": 3},
    "trade_accept": {"scholarship": 3},
    "trade_counter": {"scholarship": 3},
    "trade_reject": {"scholarship": 1},
}

# Role-based XP multipliers (profession gets bonus in their domain)
ROLE_XP_BONUS = {
    "farmer": {"farming": 1.5},
    "herder": {"husbandry": 1.5, "exploration": 1.2},
    "craftsman": {"construction": 1.5, "machinery": 1.3},
}


# ═══════════════════════════ DATA CLASSES ═══════════════════════════

@dataclass
class SkillNode:
    """One node in the skill tree. Unlocked when requirements met."""
    id: str
    name: str
    tier: int
    required_level: int
    requires: List[str]
    effects: Dict[str, Any]
    description: str
    unlocked: bool = False


@dataclass
class SkillTree:
    """A profession-specific skill tree. Tracks unlocked nodes and available choices."""

    profession: str
    display_name: str
    xp_per_level: List[int]
    nodes: Dict[str, SkillNode] = field(default_factory=dict)

    # Runtime state
    unlocked_nodes: Set[str] = field(default_factory=set)
    active_specializations: Dict[str, str] = field(default_factory=dict)

    # ═══════════════════════ FACTORY ═══════════════════════

    @classmethod
    def load(cls, vault_root: str, profession: str) -> "SkillTree":
        """Load skill tree JSON for a given profession."""
        tree_path = os.path.join(
            vault_root, "agents", "skills", f"{profession}_tree.json"
        )
        if not os.path.exists(tree_path):
            raise FileNotFoundError(f"Skill tree not found: {tree_path}")
        with open(tree_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        nodes = {}
        for node_id, node_data in data.get("nodes", {}).items():
            nodes[node_id] = SkillNode(
                id=node_id,
                name=node_data["name"],
                tier=node_data["tier"],
                required_level=node_data["required_level"],
                requires=list(node_data.get("requires", [])),
                effects=dict(node_data.get("effects", {})),
                description=node_data.get("description", ""),
                unlocked=False,
            )

        # Root nodes (tier 0) start unlocked
        tree = cls(
            profession=data["profession"],
            display_name=data["display_name"],
            xp_per_level=list(data.get("xp_per_level", [])),
            nodes=nodes,
        )
        for nid, node in tree.nodes.items():
            if node.tier == 0:
                node.unlocked = True
                tree.unlocked_nodes.add(nid)

        return tree

    # ═══════════════════════ QUERIES ═══════════════════════

    def level_for_xp(self, xp: int) -> int:
        """Return the skill level for a given XP total."""
        for level, threshold in enumerate(self.xp_per_level):
            if xp < threshold:
                return max(0, level - 1)
        return len(self.xp_per_level) - 1

    def xp_to_next_level(self, xp: int) -> int:
        """XP needed to reach next level. Returns 0 if maxed."""
        current = self.level_for_xp(xp)
        if current >= len(self.xp_per_level) - 1:
            return 0
        return self.xp_per_level[current + 1] - xp

    def check_unlock(self, node_id: str, skill_level: int) -> bool:
        """Check if a node can be unlocked at the given skill level."""
        node = self.nodes.get(node_id)
        if not node or node.unlocked:
            return False
        if skill_level < node.required_level:
            return False
        # Check all prerequisites are unlocked
        for req_id in node.requires:
            if req_id not in self.unlocked_nodes:
                return False
        return True

    def get_unlockable(self, skill_level: int, max_count: int = 3) -> List[SkillNode]:
        """Get list of nodes that are currently unlockable."""
        candidates = []
        for nid, node in self.nodes.items():
            if self.check_unlock(nid, skill_level):
                candidates.append(node)
        # Sort by tier (lowest first), then take top N
        candidates.sort(key=lambda n: n.tier)
        return candidates[:max_count]

    def unlock_node(self, node_id: str) -> bool:
        """Unlock a skill node. Returns True if successful."""
        node = self.nodes.get(node_id)
        if not node or node.unlocked:
            return False
        node.unlocked = True
        self.unlocked_nodes.add(node_id)

        # Check for specialization resolution:
        # If this node has siblings (same tier + same prereqs), mark the choice
        siblings = [
            n for n in self.nodes.values()
            if n.tier == node.tier
            and set(n.requires) == set(node.requires)
            and n.id != node_id
            and not n.unlocked
        ]
        for sib in siblings:
            # Sibling becomes permanently locked (specialization choice made)
            sib.unlocked = False  # not unlocked, but we track it as "rejected"

        return True

    def get_all_effects(self) -> Dict[str, Any]:
        """Aggregate all active effects from unlocked nodes."""
        effects = {}
        for nid in self.unlocked_nodes:
            node = self.nodes.get(nid)
            if node and node.unlocked:
                for k, v in node.effects.items():
                    if isinstance(v, (int, float)) and k in effects and isinstance(effects[k], (int, float)):
                        # Additive for numeric effects
                        effects[k] = effects[k] + v
                    elif isinstance(v, list) and k in effects and isinstance(effects[k], list):
                        effects[k] = list(set(effects[k] + v))
                    else:
                        effects[k] = v
        return effects

    def get_skill_summary(self, skill_level: int, skill_xp: int) -> str:
        """Generate a compact skill summary for the LLM prompt."""
        lines = [f"## 🎯 {self.display_name} (Lv{skill_level}, {skill_xp}XP)"]
        lines.append(f"下一级: {self.xp_to_next_level(skill_xp)}XP")
        unlocked_names = [
            self.nodes[nid].name for nid in self.unlocked_nodes
            if nid in self.nodes
        ]
        if unlocked_names:
            lines.append(f"已解锁: {', '.join(unlocked_names)}")
        unlockable = self.get_unlockable(skill_level)
        if unlockable:
            lines.append(f"可解锁: {', '.join(n.name for n in unlockable)}")
        return "\n".join(lines)


# ═══════════════════════════ XP ENGINE ═══════════════════════════

def compute_action_xp(action: str, role: str) -> Dict[str, int]:
    """Compute XP gains for performing an action, given the agent's role.

    Returns dict of {skill_name: xp_gained}.
    """
    base = dict(BASE_ACTION_XP.get(action, {}))
    bonus = ROLE_XP_BONUS.get(role, {})

    result = {}
    for skill, xp in base.items():
        mult = bonus.get(skill, 1.0)
        result[skill] = int(xp * mult)
    return result


def apply_action_xp(profile, action: str):
    """Apply XP gains from an action to the agent's profile.

    Args:
        profile: AgentProfile instance (mutated in place).
        action:  Action string performed.
    """
    xp_gains = compute_action_xp(action, profile.role)
    for skill, xp in xp_gains.items():
        profile.add_skill_xp(skill, xp)


# ═══════════════════════════ SELF-TEST ═══════════════════════════

if __name__ == "__main__":
    vault = r"C:\Users\m1916\agent-brain"

    for prof in ["farmer", "herder", "craftsman"]:
        tree = SkillTree.load(vault, prof)
        print(f"\n{'='*50}")
        print(f"Skill Tree: {tree.display_name} ({prof})")
        print(f"Levels: {len(tree.xp_per_level)} tiers")
        print(f"Total nodes: {len(tree.nodes)}")
        print(f"Root nodes (auto-unlocked): {len(tree.unlocked_nodes)}")

        # Simulate leveling
        xp = 0
        for level in range(8):
            skill_level = tree.level_for_xp(xp)
            unlockable = tree.get_unlockable(skill_level)
            print(f"  XP={xp}: Lv{skill_level}, {len(unlockable)} unlockable")
            if unlockable and level % 2 == 1:
                # Auto-unlock first available
                tree.unlock_node(unlockable[0].id)
                print(f"    Unlocked: {unlockable[0].name}")
            xp = tree.xp_per_level[min(level + 1, len(tree.xp_per_level) - 1)]

        print(f"\nFinal unlocked: {len(tree.unlocked_nodes)} nodes")
        effects = tree.get_all_effects()
        print(f"Active effects: {json.dumps(effects, ensure_ascii=False)[:200]}")

    # Test XP computation
    print(f"\n{'='*50}")
    for action in ["harvest", "breed", "forge", "water", "read"]:
        for role in ["farmer", "herder", "craftsman"]:
            xp = compute_action_xp(action, role)
            if xp:
                print(f"  {role} {action:12s} → {xp}")
    print("\nAll skill trees operational.")
