"""
sense_compiler.py — Data-driven sensory report compiler for Agent Farm
======================================================================
Replaces hardcoded if/elif chains in agent_world_local.py with a
deterministic rule engine backed by sensory_dictionary.json.

Design principles:
  1. Same physics → same words. No randomness, no hallucination.
  2. Rules are JSON data, not Python code. Edit without touching the engine.
  3. perception_bias parameter reserved for skill-differentiated agents.
  4. All template formatting happens here; callers just pass raw values.

Usage:
    compiler = SenseCompiler("sensory_dictionary.json")
    observations = compiler.compile_full_report(farm_dict)
"""
import json
import os
import copy


class SenseCompiler:
    """Compiles raw farm physics into natural-language sensory observations.

    Loads a sensory mapping dictionary (JSON), matches rules against
    farm state, and produces a list of NL observation strings suitable
    for LLM context injection.

    The compiler has three compilation levels:
      - compile_tiles(): per-crop-position observations (moisture, NPK, growth...)
      - compile_livestock(): per-animal observations (health, hunger, products...)
      - compile_weather(): global weather alerts

    compile_full_report() runs all three and returns the combined result.
    """

    # ── Match types supported by the rule engine ──
    MATCH_RANGE = "range"
    MATCH_BOOLEAN = "boolean"
    MATCH_BOOLEAN_INVERT = "boolean_invert"
    MATCH_EQUALS = "equals"

    def __init__(self, dictionary_path=None, perception_bias=0):
        """Initialize the compiler.

        Args:
            dictionary_path: Path to sensory_dictionary.json. If None, looks for
                            'sensory_dictionary.json' in the same directory.
            perception_bias: Skill modifier (0.0 = expert, positive values shift
                            perception thresholds). Reserved for future use.
        """
        if dictionary_path is None:
            dictionary_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "sensory_dictionary.json"
            )
        self.dict = self._load_dictionary(dictionary_path)
        self.perception_bias = perception_bias
        self._validate_dictionary()

    # ═══════════════════════ PUBLIC API ═══════════════════════

    def compile_full_report(self, farm):
        """Generate complete sensory report for the entire farm.

        Args:
            farm: Farm state dictionary (as returned by API)

        Returns:
            List of NL observation strings, ready for LLM context.
        """
        obs = []
        obs.extend(self.compile_tiles(farm))
        obs.extend(self.compile_livestock(farm.get("livestock", [])))
        obs.extend(self.compile_weather(farm))
        return obs

    def compile_tiles(self, farm):
        """Compile per-crop-tile observations.

        Examines soil moisture, NPK nutrients, topsoil depth, frost damage,
        and growth stage for each planted crop tile. Limited to first 8 crops
        to avoid context overflow.

        Args:
            farm: Farm state dictionary

        Returns:
            List of NL observation strings for crop tiles.
        """
        obs = []
        tile_groups = self.dict.get("tile_observations", {})
        crops = farm.get("crops", [])[:8]

        if not crops or not tile_groups:
            return obs

        # Extract grids once for all tiles
        terrain = farm.get("terrain", {})
        moisture_grid = terrain.get("soil_moisture", [])
        npk_grid = terrain.get("soil_npk", [])
        topsoil_grid = terrain.get("topsoil_depth", [])

        for c in crops:
            px = c.get("position_x", 0)
            py = c.get("position_y", 0)
            cn = c.get("crop_name", c.get("crop_type", "?"))
            gdd = c.get("gdd_percent", 0)

            # Build context for template substitution
            ctx = {
                "px": px, "py": py, "cn": cn, "gdd": gdd,
                "moisture": self._grid_get(moisture_grid, px, py, 50),
                "ts": self._grid_get(topsoil_grid, px, py, 20),
                "frost_damaged": c.get("frost_damaged", False),
            }

            # NPK values
            npk = None
            if npk_grid and px < len(npk_grid) and py < len(npk_grid[0]):
                npk = npk_grid[px][py]
            ctx["n_val"] = npk.get("N", 30) if npk else 30
            ctx["p_val"] = npk.get("P", 30) if npk else 30
            ctx["om"] = npk.get("organic_matter", 10) if npk else 10

            # Run each tile observation group against this tile
            for group_key, group_def in tile_groups.items():
                if group_key.startswith("_"):
                    continue  # skip metadata keys
                result = self._match_rules(group_def, ctx, ctx)
                if result:
                    obs.append(result)

        return obs

    def compile_livestock(self, animals):
        """Compile per-animal observations.

        Priority order: sick → poor health → product ready → feeding needed.
        Only the first matching condition per animal fires (most urgent wins).

        Args:
            animals: List of animal state dicts

        Returns:
            List of NL observation strings for animals.
        """
        obs = []
        livestock_groups = self.dict.get("livestock_observations", {})

        if not animals or not livestock_groups:
            return obs

        for a in animals[:5]:
            name = a.get("name", "?")
            species = a.get("species", "?")
            health = a.get("health", 100)

            ctx = {
                "name": name, "species": species, "health": health,
                "sick": a.get("sick", False),
                "product_ready": a.get("product_ready", False),
                "fed_today": a.get("fed_today", True),
            }

            # Priority-ordered: first match wins (most urgent)
            priority_order = [
                "health_critical", "health_poor", "product_ready", "feeding_needed"
            ]
            for group_key in priority_order:
                group_def = livestock_groups.get(group_key)
                if not group_def or group_key.startswith("_"):
                    continue
                result = self._match_rules(group_def, ctx, ctx)
                if result:
                    obs.append(result)
                    break  # only the most urgent per animal

        return obs

    def compile_weather(self, farm):
        """Compile global weather alerts.

        Unlike tiles and livestock, ALL matching weather alerts fire
        (multiple simultaneous weather conditions are possible).

        Args:
            farm: Farm state dictionary

        Returns:
            List of NL weather alert strings.
        """
        obs = []
        weather_groups = self.dict.get("weather_alerts", {})

        if not weather_groups:
            return obs

        ws = farm.get("weather_state", {})
        weather = farm.get("weather", "sunny")

        ctx = {
            "weather": weather,
            "frost_warning": ws.get("frost_warning", False),
        }

        for group_key, group_def in weather_groups.items():
            if group_key.startswith("_"):
                continue
            result = self._match_rules(group_def, ctx, ctx)
            if result:
                obs.append(result)

        return obs

    # ═══════════════════════ RULE MATCHING ENGINE ═══════════════════════

    def _match_rules(self, group_def, match_ctx, template_ctx):
        """Match a single observation group's rules against context.

        Args:
            group_def: Observation group definition from JSON
            match_ctx: Dict of values to match rules against
            template_ctx: Dict of values for template substitution

        Returns:
            Formatted NL string if a rule matches, None otherwise.
        """
        match_type = group_def.get("match_type", "range")
        field = group_def.get("field", "")
        rules = group_def.get("rules", [])

        if not rules or field not in match_ctx:
            return None

        value = match_ctx[field]

        for rule in rules:
            if self._rule_matches(match_type, rule, value):
                template = rule.get("template", "")
                if not template:
                    return None

                # Format the template with context variables
                try:
                    formatted = template
                    for key, val in template_ctx.items():
                        placeholder = "{" + key + "}"
                        if placeholder in formatted:
                            if isinstance(val, float):
                                formatted = formatted.replace(
                                    placeholder, f"{val:.1f}"
                                )
                            else:
                                formatted = formatted.replace(
                                    placeholder, str(val)
                                )
                except Exception:
                    formatted = template

                return formatted

        return None

    def _rule_matches(self, match_type, rule, value):
        """Check if a single rule matches a value.

        Args:
            match_type: One of MATCH_RANGE, MATCH_BOOLEAN, etc.
            rule: Rule dict from JSON
            value: Actual value to check

        Returns:
            True if the rule matches.
        """
        if match_type == self.MATCH_RANGE:
            rng = rule.get("range", [0, 100])
            if len(rng) >= 2:
                return rng[0] <= value < rng[1]
            return False

        elif match_type == self.MATCH_BOOLEAN:
            return bool(value) == bool(rule.get("value", True))

        elif match_type == self.MATCH_BOOLEAN_INVERT:
            # Match when value is False (e.g., fed_today=False → "not fed")
            return not bool(value)

        elif match_type == self.MATCH_EQUALS:
            return str(value) == str(rule.get("value", ""))

        return False

    # ═══════════════════════ HELPERS ═══════════════════════

    @staticmethod
    def _grid_get(grid, x, y, default=0):
        """Safely read from a 2D grid, returning default on out-of-bounds."""
        try:
            if grid and x < len(grid) and y < len(grid[x]):
                return grid[x][y]
        except (IndexError, TypeError):
            pass
        return default

    def _load_dictionary(self, path):
        """Load and parse the sensory dictionary JSON file."""
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Sensory dictionary not found: {path}"
            )
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data

    def _validate_dictionary(self):
        """Basic structural validation of the loaded dictionary."""
        if "version" not in self.dict:
            raise ValueError("Sensory dictionary missing 'version' field")
        required_sections = ["tile_observations", "livestock_observations", "weather_alerts"]
        for section in required_sections:
            if section not in self.dict:
                raise ValueError(
                    f"Sensory dictionary missing required section: '{section}'"
                )

    def reload_dictionary(self, path=None):
        """Hot-reload the dictionary (useful when editing rules live)."""
        if path is None:
            path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "sensory_dictionary.json"
            )
        self.dict = self._load_dictionary(path)
        self._validate_dictionary()

    def get_rule_stats(self):
        """Return summary stats about loaded rules."""
        stats = {}
        for section_key in ["tile_observations", "livestock_observations", "weather_alerts"]:
            section = self.dict.get(section_key, {})
            count = sum(
                1 for k, v in section.items()
                if not k.startswith("_") and isinstance(v, dict)
            )
            stats[section_key] = count
        return stats


# ═══════════════════════ BACKWARD-COMPAT WRAPPER ═══════════════════════
# This function replicates the exact signature and return format of the
# original sensory_report() in agent_world_local.py. Drop-in replacement.

_default_compiler = None

def _get_compiler():
    """Lazy-init singleton compiler (avoids loading JSON on module import)."""
    global _default_compiler
    if _default_compiler is None:
        dict_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "sensory_dictionary.json"
        )
        _default_compiler = SenseCompiler(dict_path)
    return _default_compiler


def sensory_report(farm):
    """Backward-compatible wrapper. Same signature, same output format.
    Drop-in replacement for agent_world_local.sensory_report().
    """
    return _get_compiler().compile_full_report(farm)
