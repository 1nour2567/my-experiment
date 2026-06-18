"""
rumor_engine.py — Rumor generation + verification for Agent Farm
=================================================================
Phase W4+: Agents live in a world of incomplete and sometimes wrong
information. Rumors spread through social interaction and exploration.
Some are true, some are false. Only direct observation reveals the truth.

Design:
  - Rumor templates: biome-specific claims, some accurate, some not.
  - Generation: on social_msg, explore, or idle reflection.
  - Verification: when agent explores a tile, all rumors about it are resolved.
  - Confidence: agent's belief level decays if never verified.
"""
import random
from knowledge_map import Rumor, KnowledgeMap

# ═══════════════════════════ RUMOR TEMPLATES ═══════════════════════════
# Each template: {claim, biome_restriction, accuracy (true/false/random), min_distance}

RUMOR_TEMPLATES = [
    # ── Accurate observations ──
    {
        "claim": "远处有水源的迹象——树木特别茂密",
        "biome": "riverbank",
        "accuracy": "true",
        "source_types": ["exploration", "social"],
    },
    {
        "claim": "这里的牧草长得特别旺盛，羊一定喜欢",
        "biome": "grassland",
        "accuracy": "true",
        "source_types": ["exploration", "social"],
    },
    {
        "claim": "森林深处据说生长着珍贵的药材",
        "biome": "forest",
        "accuracy": "true" if random.random() < 0.6 else "false",
        "source_types": ["social", "suspicion"],
    },
    {
        "claim": "山里的石头颜色很深，可能有铁矿",
        "biome": "hills_mountains",
        "accuracy": "true" if random.random() < 0.5 else "false",
        "source_types": ["exploration", "social", "suspicion"],
    },

    # ── Likely-false or exaggerated ──
    {
        "claim": "北边山里有座金矿——老辈人都这么说",
        "biome": "hills_mountains",
        "accuracy": "false",
        "source_types": ["social", "overheard"],
    },
    {
        "claim": "那片草原上有野马群，驯服了能卖大价钱",
        "biome": "grassland",
        "accuracy": "false",
        "source_types": ["social", "overheard"],
    },
    {
        "claim": "森林里那片空地下面埋着宝藏——一个老猎人说的",
        "biome": "forest",
        "accuracy": "false",
        "source_types": ["social", "overheard"],
    },
    {
        "claim": "这块湿地底下全是泥炭，挖出来能当燃料卖",
        "biome": "wetland",
        "accuracy": "true" if random.random() < 0.5 else "false",
        "source_types": ["exploration", "social"],
    },
    {
        "claim": "冲积平原的土壤特别肥，种什么都能大丰收",
        "biome": "alluvial_plain",
        "accuracy": "true",
        "source_types": ["social", "suspicion"],
    },
    {
        "claim": "河岸边的淤泥里扒一扒，能扒出好看的石头",
        "biome": "riverbank",
        "accuracy": "true" if random.random() < 0.4 else "false",
        "source_types": ["exploration", "suspicion"],
    },

    # ── Suspicion (self-generated hunches) ──
    {
        "claim": "你总觉得东边那片地底下有什么东西——每次走过都有种奇怪的感觉",
        "biome": None,  # any biome
        "accuracy": "false",
        "source_types": ["suspicion"],
    },
    {
        "claim": "远处的山看着不太对劲——或许值得爬上去看看",
        "biome": "hills_mountains",
        "accuracy": "true",
        "source_types": ["suspicion"],
    },
]


class RumorEngine:
    """Generates, tracks, and resolves rumors."""

    def __init__(self):
        pass

    def generate_rumor(self, source_type: str, biome: str,
                       x: int = -1, y: int = -1,
                       source_name: str = "unknown") -> Rumor:
        """Generate a rumor appropriate for a biome and source type.

        Args:
            source_type: 'exploration', 'social', 'overheard', 'suspicion'
            biome: current biome context
            x, y: tile coordinates (-1 = not location-specific)
            source_name: who/what generated the rumor

        Returns:
            A Rumor object, or None if no appropriate template found.
        """
        # Filter templates by source type compatibility
        candidates = [
            t for t in RUMOR_TEMPLATES
            if source_type in t.get("source_types", [])
            and (t["biome"] is None or t["biome"] == biome)
        ]
        if not candidates:
            # Fallback: any template that matches source type
            candidates = [
                t for t in RUMOR_TEMPLATES
                if source_type in t.get("source_types", [])
            ]
        if not candidates:
            return None

        template = random.choice(candidates)
        accuracy = template["accuracy"]
        if accuracy == "true":
            claim_truth = True
        elif accuracy == "false":
            claim_truth = False
        else:
            claim_truth = random.random() < 0.5

        # Confidence depends on source type
        confidence_map = {
            "exploration": random.uniform(0.5, 0.8),
            "social": random.uniform(0.4, 0.7),
            "overheard": random.uniform(0.2, 0.5),
            "suspicion": random.uniform(0.3, 0.5),
        }
        confidence = confidence_map.get(source_type, 0.5)

        rumor_id = f"rumor_{source_type}_{x}_{y}_{random.randint(1000,9999)}"
        return Rumor(
            rumor_id=rumor_id,
            claim=template["claim"],
            source=source_name,
            tile_x=x,
            tile_y=y,
            confidence=confidence,
            is_true=None,  # unverified
        )

    def on_explore(self, knowledge_map: KnowledgeMap,
                   x: int, y: int, biome: str,
                   reality: dict, source_name: str = "exploration") -> list:
        """Called when agent explores a tile. Returns resolution messages."""
        msgs = []

        # 1. Resolve any existing rumors about this tile
        resolution_msgs = knowledge_map.verify_rumors_at(x, y, reality)
        msgs.extend(resolution_msgs)

        # 2. Possibly generate a new rumor about the area
        if random.random() < 0.3:  # 30% chance
            rumor = self.generate_rumor("exploration", biome, x, y, source_name)
            if rumor:
                knowledge_map.add_rumor(rumor)

        return msgs

    def on_social_interaction(self, knowledge_map: KnowledgeMap,
                              other_agent_id: str, other_biome: str) -> Rumor:
        """Called after social_msg. May generate a rumor from the conversation.
        Returns the rumor if one was created, else None."""
        if random.random() < 0.25:  # 25% chance of hearing a rumor
            rumor = self.generate_rumor(
                "social", other_biome,
                source_name=other_agent_id
            )
            if rumor:
                knowledge_map.add_rumor(rumor)
                return rumor
        return None

    def idle_reflection(self, knowledge_map: KnowledgeMap,
                        current_biome: str) -> Rumor:
        """Called occasionally (every ~30 cycles). Agent may develop a hunch.
        Returns the rumor if one was created, else None."""
        if random.random() < 0.1:  # 10% chance
            rumor = self.generate_rumor(
                "suspicion", current_biome,
                x=random.randint(10, 40),
                y=random.randint(10, 40),
                source_name="自己的直觉"
            )
            if rumor:
                knowledge_map.add_rumor(rumor)
                return rumor
        return None

    def rumor_stats(self, knowledge_map: KnowledgeMap) -> dict:
        """Return summary stats about the agent's rumor collection."""
        total = len(knowledge_map.rumors)
        unverified = sum(1 for r in knowledge_map.rumors if r.is_true is None)
        confirmed = sum(1 for r in knowledge_map.rumors if r.is_true is True)
        busted = sum(1 for r in knowledge_map.rumors if r.is_true is False)
        return {
            "total": total,
            "unverified": unverified,
            "confirmed": confirmed,
            "busted": busted,
        }


# ═══════════════════════════ SELF-TEST ═══════════════════════════

if __name__ == "__main__":
    engine = RumorEngine()
    kmap = KnowledgeMap(agent_id="test_agent")

    # Test rumor generation for each biome
    for biome in ["forest", "grassland", "hills_mountains", "wetland", "riverbank", "alluvial_plain"]:
        rumor = engine.generate_rumor("social", biome, x=15, y=20, source_name="test_source")
        if rumor:
            kmap.add_rumor(rumor)

    print(f"Generated {len(kmap.rumors)} rumors across 6 biomes")
    stats = engine.rumor_stats(kmap)
    assert stats["total"] >= 1, "Should have at least 1 rumor"
    print(f"  Unverified: {stats['unverified']}")

    # Test verification
    reality = {"biome": "hills_mountains", "resource": "iron_ore"}
    msgs = kmap.verify_rumors_at(15, 20, reality)
    print(f"Verification at (15,20): {len(msgs)} resolutions")
    verified_count = sum(1 for r in kmap.rumors if r.is_true is not None)
    print(f"  Verified: {verified_count}/{len(kmap.rumors)}")

    stats2 = engine.rumor_stats(kmap)
    assert stats2["unverified"] < stats["total"], "Some rumors should now be verified"
    print(f"After verify: {stats2['confirmed']} confirmed, {stats2['busted']} busted, {stats2['unverified']} unverified")

    print("\nAll rumor engine tests passed!")
