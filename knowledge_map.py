"""
knowledge_map.py — Personal knowledge graph for Agent Farm
============================================================
Phase W1-3: Each agent maintains their own KnowledgeMap — a private,
incomplete model of the world. Tiles are discovered gradually with
three precision tiers, facts are learned from experience or books,
and other agents are perceived through social interaction.

Design principles:
  1. No agent has global truth. Everything is filtered through their map.
  2. Tiles have three observation tiers: distant → mid → close.
  3. Stale observations degrade: "You haven't been there in a while."
  4. Facts can be wrong (rumor system — future Phase).
"""
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Set, Any
import json, os, time


# ═══════════════════════════ DATA CLASSES ═══════════════════════════

@dataclass
class TileKnowledge:
    """What an agent knows about a specific map tile. All fields optional —
    if None, agent hasn't observed that property yet."""
    pos_x: int
    pos_y: int
    last_observed_cycle: int = 0

    # ── Distant (5-15 tiles away) ──
    biome: Optional[str] = None
    terrain_desc: Optional[str] = None  # "深色森林", "开阔平原"
    has_building: Optional[bool] = None

    # ── Mid (2-5 tiles) ──
    tree_species: Optional[str] = None  # "橡树和桦树"
    animal_signs: Optional[str] = None   # "鹿的足迹"
    water_nearby: Optional[bool] = None

    # ── Close (0-2 tiles) ──
    soil_type: Optional[str] = None
    moisture_estimate: Optional[float] = None  # ±10% error
    npk_estimate: Optional[Dict[str, int]] = None
    topsoil_estimate: Optional[float] = None
    wood_quality: Optional[str] = None
    hidden_resource: Optional[str] = None  # only after explore/cut actions

    # ── Special ──
    landmark_name: Optional[str] = None  # agent can name places
    personal_notes: Optional[str] = None  # free-text note


@dataclass
class AgentPerception:
    """What this agent knows about another agent."""
    agent_id: str
    display_name: str = ""
    role: str = ""
    last_seen_cycle: int = 0
    trust_level: float = 0.5  # 0-1, affects rumor belief
    known_skills: Dict[str, int] = field(default_factory=dict)
    shared_facts: List[str] = field(default_factory=list)


@dataclass
class Rumor:
    """A potentially false belief about the world. Only verified when observed."""
    rumor_id: str
    claim: str                    # e.g. "北边森林深处有一片古老的蘑菇地"
    source: str                   # "old_wang", "overheard", "book", "suspicion"
    tile_x: int = -1              # target tile, -1 = not location-specific
    tile_y: int = -1
    confidence: float = 0.6       # how much the agent believes this
    is_true: Optional[bool] = None  # None = unverified, True/False = verified
    verified_cycle: int = 0


# ═══════════════════════════ KNOWLEDGE MAP ═══════════════════════════

@dataclass
class KnowledgeMap:
    """An agent's private, incomplete model of the world."""

    agent_id: str

    # ── Spatial knowledge ──
    known_tiles: Dict[str, TileKnowledge] = field(default_factory=dict)
    # key: "x,y"

    # ── Global facts ──
    discovered_facts: List[Dict[str, str]] = field(default_factory=list)
    # Each: {"id": "grassland_pasture", "description": "草原适合放牧", "source": "book"|"experience"|"social"}

    # ── Social knowledge ──
    agent_perceptions: Dict[str, AgentPerception] = field(default_factory=dict)

    # ── Rumors & false beliefs (Phase W4+) ──
    rumors: List[Rumor] = field(default_factory=list)

    # ═══════════════════════ TILE METHODS ═══════════════════════

    def observe_tile(self, x: int, y: int, distance_tiles: int,
                     raw_tile_data: Dict[str, Any], cycle: int):
        """Record an observation of a tile at a given distance.

        Args:
            x, y: tile coordinates
            distance_tiles: how far the agent is from the tile
            raw_tile_data: full tile data dict from the server
            cycle: current game cycle number
        """
        key = f"{x},{y}"
        if key not in self.known_tiles:
            self.known_tiles[key] = TileKnowledge(pos_x=x, pos_y=y)

        tk = self.known_tiles[key]
        tk.last_observed_cycle = cycle

        if distance_tiles <= 2:
            self._fill_close(tk, raw_tile_data)
        elif distance_tiles <= 5:
            self._fill_mid(tk, raw_tile_data)
        else:
            self._fill_distant(tk, raw_tile_data)

    def _fill_distant(self, tk: TileKnowledge, data: dict):
        """Fill distant-tier observations (biome only)."""
        tk.biome = data.get("biome")
        tk.has_building = data.get("has_building")
        if tk.biome == "forest":
            tk.terrain_desc = "一片深色的树林，树冠密布"
        elif tk.biome == "grassland":
            tk.terrain_desc = "开阔的草原，牧草随风起伏"
        elif tk.biome == "wetland":
            tk.terrain_desc = "低洼的湿地，芦苇丛生"
        elif tk.biome == "alluvial_plain":
            tk.terrain_desc = "平坦的冲积平原，土壤看起来肥沃"
        elif tk.biome == "hills_mountains":
            tk.terrain_desc = "起伏的丘陵地带，隐约可见岩层"
        elif tk.biome == "riverbank":
            tk.terrain_desc = "河岸地带，水流声隐约可闻"
        else:
            tk.terrain_desc = "地势平缓的普通土地"

    def _fill_mid(self, tk: TileKnowledge, data: dict):
        """Fill mid-tier observations (species, signs)."""
        self._fill_distant(tk, data)
        tk.tree_species = data.get("tree_species")
        tk.animal_signs = data.get("animal_signs")
        tk.water_nearby = data.get("water_nearby")

    def _fill_close(self, tk: TileKnowledge, data: dict):
        """Fill close-tier observations (soil, quality, resources)."""
        self._fill_mid(tk, data)
        tk.soil_type = data.get("soil_type")
        tk.moisture_estimate = data.get("soil_moisture")
        tk.npk_estimate = data.get("soil_npk")
        tk.topsoil_estimate = data.get("topsoil_depth")
        tk.wood_quality = data.get("wood_quality")
        # Hidden resources only revealed by explicit exploration or mining
        if data.get("revealed_resource"):
            tk.hidden_resource = data.get("revealed_resource")

    def get_tile_knowledge(self, x: int, y: int) -> Optional[TileKnowledge]:
        """Get what this agent knows about a tile, or None if never observed."""
        return self.known_tiles.get(f"{x},{y}")

    def get_stale_tiles(self, current_cycle: int, threshold: int = 50) -> List[TileKnowledge]:
        """Return tiles not observed in the last `threshold` cycles."""
        return [
            tk for tk in self.known_tiles.values()
            if current_cycle - tk.last_observed_cycle > threshold
        ]

    def tile_summary_for_prompt(self, x: int, y: int, current_cycle: int) -> str:
        """Generate a compact NL summary of what the agent knows about one tile."""
        tk = self.get_tile_knowledge(x, y)
        if not tk:
            return f"({x},{y}): 完全未知"

        stale = current_cycle - tk.last_observed_cycle > 50
        parts = [f"({x},{y}):"]

        if tk.biome:
            parts.append(tk.terrain_desc or tk.biome)
        if tk.soil_type:
            parts.append(f"土壤={tk.soil_type}")
        if tk.moisture_estimate is not None:
            parts.append(f"湿度≈{tk.moisture_estimate:.0f}%")
        if tk.tree_species:
            parts.append(f"树种={tk.tree_species}")
        if tk.animal_signs:
            parts.append(f"动物痕迹={tk.animal_signs}")
        if tk.hidden_resource:
            parts.append(f"🔍 发现: {tk.hidden_resource}")

        result = " ".join(parts)
        if stale:
            result += " ⚠ 已很久没去过那里了"
        return result

    # ═══════════════════════ FACT METHODS ═══════════════════════

    def add_fact(self, fact_id: str, description: str, source: str):
        """Add a discovered fact. Silently ignores duplicates."""
        if not any(f["id"] == fact_id for f in self.discovered_facts):
            self.discovered_facts.append({
                "id": fact_id,
                "description": description,
                "source": source,
            })

    def has_fact(self, fact_id: str) -> bool:
        """Check if the agent knows a specific fact."""
        return any(f["id"] == fact_id for f in self.discovered_facts)

    def facts_for_prompt(self) -> str:
        """Generate a compact summary of discovered facts for LLM context."""
        if not self.discovered_facts:
            return "(尚未发现任何全局知识)"
        lines = []
        for f in self.discovered_facts[-5:]:
            src_icon = {"book": "📖", "experience": "💡", "social": "🗣"}.get(f["source"], "•")
            lines.append(f"{src_icon} {f['description']} (来源: {f['source']})")
        return "\n".join(lines)

    # ═══════════════════════ SOCIAL METHODS ═══════════════════════

    def perceive_agent(self, other_id: str, other_name: str, other_role: str, cycle: int):
        """Record an observation of another agent."""
        if other_id not in self.agent_perceptions:
            self.agent_perceptions[other_id] = AgentPerception(
                agent_id=other_id,
                display_name=other_name,
                role=other_role,
            )
        ap = self.agent_perceptions[other_id]
        ap.last_seen_cycle = cycle

    def adjust_trust(self, other_id: str, delta: float):
        """Modify trust level toward another agent."""
        if other_id in self.agent_perceptions:
            ap = self.agent_perceptions[other_id]
            ap.trust_level = max(0.0, min(1.0, ap.trust_level + delta))

    # ═══════════════════════ RUMOR METHODS ═══════════════════════

    def add_rumor(self, rumor: Rumor):
        """Add a rumor. Silently ignores exact duplicates."""
        for r in self.rumors:
            if r.claim == rumor.claim and r.tile_x == rumor.tile_x:
                return  # duplicate
        self.rumors.append(rumor)

    def verify_rumors_at(self, x: int, y: int, reality: Dict[str, Any]) -> List[str]:
        """Verify all unverified rumors about a tile against ground truth.
        Returns list of resolution messages (e.g. 'rumor confirmed' or 'rumor busted')."""
        msgs = []
        for rumor in self.rumors:
            if rumor.tile_x != x or rumor.tile_y != y or rumor.is_true is not None:
                continue
            rumor.is_true = False  # default: busted
            # Check if the claim matches reality
            claim_lower = rumor.claim.lower()
            if reality.get("biome") == "forest" and "森林" in rumor.claim:
                rumor.is_true = True
            if reality.get("biome") == "grassland" and "草原" in rumor.claim:
                rumor.is_true = True
            if reality.get("biome") in ("hills_mountains",) and ("矿" in rumor.claim or "金" in rumor.claim):
                rumor.is_true = reality.get("resource", "") not in ("", "(nothing special)")
            if reality.get("resource") and reality["resource"] in rumor.claim:
                rumor.is_true = True
            if reality.get("water_nearby") and "水" in rumor.claim:
                rumor.is_true = True

            rumor.verified_cycle = 0  # timestamp via caller
            if rumor.is_true:
                msgs.append(f"✅ 传言证实: {rumor.claim} (此前你听{rumor.source}说的)")
            else:
                msgs.append(f"❌ 传言破灭: {rumor.claim} —— {rumor.source}说的并不准确")

        return msgs

    def unverified_rumors_snippet(self) -> str:
        """Generate a compact rumor summary for LLM context."""
        unverified = [r for r in self.rumors if r.is_true is None]
        if not unverified:
            return ""
        lines = ["## 🗣 你听说过这些传言（未经证实）"]
        for r in unverified[-5:]:
            loc = f"({r.tile_x},{r.tile_y})" if r.tile_x >= 0 else "某处"
            trust_icon = "🤔" if r.confidence < 0.4 else "👂"
            lines.append(f"- {trust_icon} {loc}: {r.claim} (来自{r.source}, 可信度{r.confidence:.0%})")
        lines.append("传言可能是假的——亲自探索才能确认真伪。")
        return "\n".join(lines)

    # ═══════════════════════ SERIALIZATION ═══════════════════════

    def save(self, vault_root: str):
        """Persist knowledge map to knowledge/map.json inside the agent's sub-vault."""
        path = os.path.join(vault_root, "knowledge", "map.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)

        data = {
            "agent_id": self.agent_id,
            "known_tiles": {
                k: asdict(v) for k, v in self.known_tiles.items()
            },
            "discovered_facts": self.discovered_facts,
            "agent_perceptions": {
                k: asdict(v) for k, v in self.agent_perceptions.items()
            },
            "rumors": [asdict(r) for r in self.rumors],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, vault_root: str, agent_id: str) -> "KnowledgeMap":
        """Load knowledge map from disk, or create a fresh one."""
        path = os.path.join(vault_root, "knowledge", "map.json")
        kmap = cls(agent_id=agent_id)

        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, td in data.get("known_tiles", {}).items():
                kmap.known_tiles[key] = TileKnowledge(**td)
            kmap.discovered_facts = data.get("discovered_facts", [])
            for aid, apd in data.get("agent_perceptions", {}).items():
                kmap.agent_perceptions[aid] = AgentPerception(**apd)
            for rd in data.get("rumors", []):
                kmap.rumors.append(Rumor(**rd))

        return kmap


# ═══════════════════════════ SELF-TEST ═══════════════════════════

if __name__ == "__main__":
    vault = r"C:\Users\m1916\agent-brain"

    kmap = KnowledgeMap(agent_id="xu_renwu")

    # Distant observation
    kmap.observe_tile(5, 10, 8, {"biome": "forest"}, cycle=1)
    tk = kmap.get_tile_knowledge(5, 10)
    assert tk.biome == "forest", f"Expected forest, got {tk.biome}"
    assert tk.soil_type is None, "Distant should not know soil"
    print("Distant: biome=forest, soil=unknown OK")

    # Mid observation
    kmap.observe_tile(5, 10, 4, {
        "biome": "forest", "tree_species": "oak_birch", "animal_signs": "deer_tracks",
    }, cycle=5)
    tk = kmap.get_tile_knowledge(5, 10)
    assert tk.tree_species == "oak_birch"
    assert tk.soil_type is None, "Mid should not know soil yet"
    print("Mid: tree_species=oak_birch, soil=unknown OK")

    # Close observation
    kmap.observe_tile(5, 10, 1, {
        "biome": "forest", "tree_species": "oak_birch", "animal_signs": "deer_tracks",
        "soil_type": "alfisol", "soil_moisture": 45, "topsoil_depth": 15,
        "wood_quality": "premium_oak",
    }, cycle=10)
    tk = kmap.get_tile_knowledge(5, 10)
    assert tk.soil_type == "alfisol"
    assert tk.moisture_estimate == 45
    assert tk.wood_quality == "premium_oak"
    print("Close: soil=alfisol, moisture=45, wood=premium OK")

    # Facts
    kmap.add_fact("grassland_pasture", "grassland good for grazing", "book")
    kmap.add_fact("forest_erosion", "deforestation = 3x erosion", "experience")
    assert len(kmap.discovered_facts) == 2
    assert kmap.has_fact("grassland_pasture")
    assert not kmap.has_fact("nonexistent")
    print("Facts: 2 discovered, has_fact checks OK")

    # Social
    kmap.perceive_agent("old_wang", "old_wang", "herder", cycle=10)
    kmap.adjust_trust("old_wang", 0.2)
    assert kmap.agent_perceptions["old_wang"].trust_level == 0.7
    print("Trust: 0.5+0.2=0.7 OK")

    # Stale tiles
    stale = kmap.get_stale_tiles(current_cycle=100, threshold=50)
    assert len(stale) == 1
    print(f"Stale tiles at cycle 100: {len(stale)} OK")

    # Save/Load roundtrip
    kmap.save(vault)
    kmap2 = KnowledgeMap.load(vault, "xu_renwu")
    assert len(kmap2.known_tiles) == len(kmap.known_tiles)
    assert len(kmap2.discovered_facts) == len(kmap.discovered_facts)
    print("Save/Load roundtrip: OK")

    # Boundary tests
    empty = KnowledgeMap(agent_id="nobody")
    assert empty.tile_summary_for_prompt(99, 99, 0) == "(99,99): 完全未知"
    assert empty.facts_for_prompt() == "(尚未发现任何全局知识)"
    assert empty.get_stale_tiles(0) == []
    print("Empty map: correct defaults")

    print("\n=== ALL 12 KnowledgeMap tests passed ===")
