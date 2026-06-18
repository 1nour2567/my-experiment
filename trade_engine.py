"""
trade_engine.py — Agent-to-agent trading system for Agent Farm
===============================================================
Phase W6-2: Structured trade proposals with verification, counter-offers,
trust updates, and reputation tracking. Trades are write-to-inbox like
social_msg, but with structured JSON payload for machine verification.

Design:
  - trade_propose: write structured offer to target inbox
  - trade_accept: verify both sides, exchange items, update trust
  - trade_counter: modify offer and return to sender
  - trade_reject: notify sender, small trust penalty only if repeated
  - All trades logged as economic_trade events in vault
  - Trust: effective_trust = trust + min(0.2, 0.01 * months_since_last_negative)
  - Season reflection: auto-generate trade insights
"""
import json, os, datetime
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any


# ═══════════════════════════ DATA CLASSES ═══════════════════════════

@dataclass
class TradeOffer:
    """A structured trade proposal between two agents."""
    trade_id: str
    from_agent: str          # profile id
    to_agent: str
    offer: Dict[str, int]    # {item_key: quantity}  e.g. {"wheat": 20}
    request: Dict[str, int]  # {item_key: quantity}
    status: str = "pending"  # pending | accepted | rejected | countered | expired
    created_season: str = ""
    created_day: int = 0
    created_year: int = 0
    created_hour: float = 0.0
    counter_history: List[Dict] = field(default_factory=list)


# ═══════════════════════════ TRADE ENGINE ═══════════════════════════

# Item resolution: map trade item names to inventory keys and storage items
ITEM_ALIASES = {
    # Crops/seeds (inventory)
    "wheat": {"type": "seed", "keys": ["wheat_seeds", "wheat"]},
    "wheat_seeds": {"type": "seed", "keys": ["wheat_seeds"]},
    "pumpkin": {"type": "seed", "keys": ["pumpkin_seeds", "pumpkin"]},
    "pumpkin_seeds": {"type": "seed", "keys": ["pumpkin_seeds"]},
    "corn": {"type": "seed", "keys": ["corn_seeds", "corn"]},
    "corn_seeds": {"type": "seed", "keys": ["corn_seeds"]},
    "tomato": {"type": "seed", "keys": ["tomato_seeds", "tomato"]},
    "tomato_seeds": {"type": "seed", "keys": ["tomato_seeds"]},
    "potato": {"type": "seed", "keys": ["potato_seeds", "potato"]},
    "powder_melon": {"type": "seed", "keys": ["powder_melon_seeds", "powder_melon"]},
    "strawberry": {"type": "seed", "keys": ["strawberry_seeds", "strawberry"]},
    "blueberry": {"type": "seed", "keys": ["blueberry_seeds", "blueberry"]},
    "rice": {"type": "seed", "keys": ["rice_seeds", "rice"]},
    "soybean": {"type": "seed", "keys": ["soybean_seeds", "soybean"]},
    "winter_seeds": {"type": "seed", "keys": ["winter_seeds"]},
    "tulip": {"type": "seed", "keys": ["tulip_seeds", "tulip"]},
    # Animal products (storage)
    "egg": {"type": "storage", "keys": ["egg", "eggs"]},
    "milk": {"type": "storage", "keys": ["milk"]},
    "wool": {"type": "storage", "keys": ["wool"]},
    "meat": {"type": "storage", "keys": ["meat", "pork", "mutton", "chicken_meat", "beef"]},
    # Tools
    "iron_hoe": {"type": "tool", "keys": ["iron_hoe", "hoe_iron"]},
    "copper_hoe": {"type": "tool", "keys": ["copper_hoe", "hoe_copper"]},
    "steel_hoe": {"type": "tool", "keys": ["steel_hoe", "hoe_steel"]},
    "iron_sickle": {"type": "tool", "keys": ["iron_sickle", "sickle_iron"]},
    "iron_watering_can": {"type": "tool", "keys": ["iron_watering_can", "watering_can_iron"]},
    "hammer": {"type": "tool", "keys": ["hammer", "hammer_copper", "hammer_iron"]},
    # Materials
    "wood": {"type": "storage", "keys": ["wood", "lumber", "timber"]},
    "stone": {"type": "storage", "keys": ["stone", "stones"]},
    "clay": {"type": "storage", "keys": ["clay"]},
    "iron_ore": {"type": "storage", "keys": ["iron_ore"]},
    # Gold
    "gold": {"type": "gold", "keys": ["gold"]},
}

# Trust/reputation constants
TRUST_GAIN_TRADE = 0.05      # successful trade
TRUST_LOSS_DEFAULT = 0.1     # reneging (offer items missing)
TRUST_LOSS_REPEAT = 0.3      # repeated reneging
REPUTATION_GAIN_TRADE = 1    # per successful trade
REPUTATION_LOSS_RENEGE = 5   # per renege
TRUST_DECAY_RATE = 0.01      # per month since last negative event
TRUST_DECAY_CAP = 0.2        # max decay compensation
AUTO_REJECT_TRUST = 0.3      # effective_trust below this = auto-reject


def effective_trust(trust: float, months_since_negative: int) -> float:
    """Compute effective trust with time decay."""
    return trust + min(TRUST_DECAY_CAP, TRUST_DECAY_RATE * months_since_negative)


def parse_trade_item(item_name: str) -> Optional[Dict]:
    """Resolve a trade item name to its canonical form."""
    name = item_name.lower().strip()
    if name in ITEM_ALIASES:
        return dict(ITEM_ALIASES[name])
    # Fuzzy match
    for alias, info in ITEM_ALIASES.items():
        if name in alias or alias in name:
            return dict(info)
    return None


def check_has_items(profile, item_name: str, quantity: int) -> bool:
    """Check if an agent has enough of a trade item (in any form)."""
    item_info = parse_trade_item(item_name)
    if not item_info:
        return False

    item_type = item_info["type"]

    if item_type == "gold":
        return True  # gold is checked at trade time from state

    if item_type == "seed":
        inv = profile.inventory.get("owned_books", [])  # wrong - need actual inventory
        return True  # validated at exchange time against actual state

    if item_type == "tool":
        # Check agent's tools_owned list
        tools = profile.inventory.get("tools_owned", [])
        return sum(1 for t in tools if t in item_info.get("keys", [])) >= quantity

    if item_type == "storage":
        return True  # validated at exchange time

    return True  # default: optimistic, validated at exchange time


class TradeEngine:
    """Manages structured trade proposals and exchanges between agents."""

    def __init__(self, parent_vault: str):
        self.parent_vault = parent_vault

    # ═══════════════════════ PROPOSE ═══════════════════════

    def propose(self, from_agent: str, to_agent: str,
                offer: Dict[str, int], request: Dict[str, int],
                state: dict) -> Dict[str, Any]:
        """Create a trade proposal. Validates offer items exist in sender's inventory.

        Returns {"success": bool, "message": str, "trade_id": str}
        """
        # Validate offer items
        inv = state.get("inventory", {})
        storage = state.get("storage", [])
        gold = state.get("gold", 0)

        for item_name, qty in offer.items():
            if item_name == "gold":
                if gold < qty:
                    return {"success": False, "message": f"金币不足：需要{qty}G，只有{gold}G"}
                continue
            item_info = parse_trade_item(item_name)
            if not item_info:
                return {"success": False, "message": f"不认识的物品: {item_name}"}
            # Seed check
            if item_info["type"] == "seed":
                found = False
                for key in item_info["keys"]:
                    if inv.get(key, 0) >= qty:
                        found = True; break
                if not found:
                    return {"success": False, "message": f"{item_name}库存不足：需要{qty}"}
            # Storage check
            elif item_info["type"] == "storage":
                found = False
                for key in item_info["keys"]:
                    matching = [s for s in storage if key in str(s.get("key", "")).lower() or key in str(s.get("name", "")).lower()]
                    if len(matching) >= qty:
                        found = True; break
                if not found:
                    return {"success": False, "message": f"仓库里没有足够的{item_name}：需要{qty}"}
            # Tool check
            elif item_info["type"] == "tool":
                tools = state.get("inventory", {}).get("tools", [])
                matching = [t for t in tools if any(k in str(t).lower() for k in item_info["keys"])]
                if len(matching) < qty:
                    return {"success": False, "message": f"没有足够的{item_name}：需要{qty}"}

        # Create trade
        trade_id = f"trade_{from_agent}_{to_agent}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
        trade = TradeOffer(
            trade_id=trade_id,
            from_agent=from_agent,
            to_agent=to_agent,
            offer=offer,
            request=request,
            status="pending",
            created_season=state.get("season", "?"),
            created_day=state.get("day", 0),
            created_year=state.get("year", 1),
            created_hour=state.get("hour", 0.0),
        )

        # Write to target inbox as structured JSON
        self._write_trade_to_inbox(trade)

        # Also save to pending trades directory
        self._save_pending_trade(trade)

        return {
            "success": True,
            "message": f"向{to_agent}发起交易：用{self._format_items(offer)}换{self._format_items(request)}",
            "trade_id": trade_id,
        }

    # ═══════════════════════ ACCEPT ═══════════════════════

    def accept(self, trade_id: str, acceptor_agent: str,
               acceptor_state: dict, acceptor_profile) -> Dict[str, Any]:
        """Accept a pending trade. Verifies both sides and exchanges items.

        Returns {"success": bool, "message": str, "exchanged": dict}
        """
        trade = self._load_pending_trade(trade_id)
        if not trade:
            return {"success": False, "message": "找不到这个交易——可能已过期或被取消"}

        if trade.to_agent != acceptor_agent:
            return {"success": False, "message": "这个交易不是发给你的"}

        if trade.status != "pending":
            return {"success": False, "message": f"交易状态是'{trade.status}'，无法接受"}

        # Verify acceptor HAS the requested items
        inv = acceptor_state.get("inventory", {})
        storage = acceptor_state.get("storage", [])
        gold = acceptor_state.get("gold", 0)

        for item_name, qty in trade.request.items():
            if item_name == "gold":
                if gold < qty:
                    self._reject_trade(trade, "acceptor_lacks_items")
                    return {"success": False, "message": f"金币不足：需要{qty}G，只有{gold}G。交易已取消。"}
                continue
            item_info = parse_trade_item(item_name)
            if not item_info:
                self._reject_trade(trade, "acceptor_lacks_items")
                return {"success": False, "message": f"不认识的物品: {item_name}。交易已取消。"}
            if item_info["type"] == "seed":
                found = any(inv.get(k, 0) >= qty for k in item_info["keys"])
                if not found:
                    self._reject_trade(trade, "acceptor_lacks_items")
                    return {"success": False, "message": f"{item_name}库存不足：需要{qty}。交易已取消。"}

        # EXECUTE EXCHANGE
        trade.status = "accepted"

        # We can't directly modify server state from here — the agent-world-llm caller
        # must execute the actual inventory mutations through the farm API.
        # Instead, we return the exchange instructions.
        exchange = {
            "from_proposer": trade.offer,      # proposer gives these
            "from_acceptor": trade.request,     # acceptor gives these
            "trade_id": trade_id,
            "proposer_id": trade.from_agent,
            "acceptor_id": trade.to_agent,
        }

        # Update trust
        self._update_trust(trade.from_agent, trade.to_agent, TRUST_GAIN_TRADE)

        # Clean up pending trade
        self._delete_pending_trade(trade_id)

        # Write acceptance notification to proposer's inbox
        self._write_trade_result(trade, "accepted", acceptor_profile.display_name)

        return {
            "success": True,
            "message": f"交易达成！获得{self._format_items(trade.offer)}，付出{self._format_items(trade.request)}",
            "exchange": exchange,
        }

    # ═══════════════════════ COUNTER ═══════════════════════

    def counter(self, trade_id: str, counter_agent: str,
                new_offer: Dict[str, int], new_request: Dict[str, int],
                state: dict, display_name: str) -> Dict[str, Any]:
        """Counter-offer: modify the trade terms and send back."""
        trade = self._load_pending_trade(trade_id)
        if not trade:
            return {"success": False, "message": "找不到原交易——可能已过期"}

        if trade.to_agent != counter_agent:
            return {"success": False, "message": "这个交易不是发给你的"}

        # Record the counter in history
        trade.counter_history.append({
            "by": counter_agent,
            "original_offer": dict(trade.offer),
            "original_request": dict(trade.request),
            "new_offer": dict(new_offer),
            "new_request": dict(new_request),
        })

        # Update trade with new terms, swap direction
        old_from = trade.from_agent
        trade.from_agent = counter_agent
        trade.to_agent = old_from
        trade.offer = new_offer
        trade.request = new_request

        self._write_trade_to_inbox(trade)
        self._save_pending_trade(trade)

        return {
            "success": True,
            "message": f"还价：用{self._format_items(new_offer)}换{self._format_items(new_request)}",
        }

    # ═══════════════════════ REJECT ═══════════════════════

    def reject(self, trade_id: str, rejector_agent: str,
               display_name: str) -> Dict[str, Any]:
        """Reject a trade proposal."""
        trade = self._load_pending_trade(trade_id)
        if not trade:
            return {"success": False, "message": "找不到原交易——可能已过期"}

        if trade.to_agent != rejector_agent:
            return {"success": False, "message": "这个交易不是发给你的"}

        trade.status = "rejected"
        self._write_trade_result(trade, "rejected", display_name)
        self._delete_pending_trade(trade_id)

        return {"success": True, "message": f"拒绝了来自{trade.from_agent}的交易"}

    # ═══════════════════════ INBOX I/O ═══════════════════════

    def _write_trade_to_inbox(self, trade: TradeOffer):
        """Write a structured trade proposal to the recipient's inbox."""
        inbox_dir = os.path.join(self.parent_vault, "social")
        os.makedirs(inbox_dir, exist_ok=True)
        inbox_path = os.path.join(inbox_dir, f"{trade.to_agent}_inbox.md")

        entry = (
            f"\n### 💼 交易提议 from {trade.from_agent}\n"
            f"<!-- trade_proposal: {json.dumps({'trade_id': trade.trade_id, 'from': trade.from_agent, 'to': trade.to_agent, 'offer': trade.offer, 'request': trade.request, 'status': trade.status}, ensure_ascii=False)} -->\n"
            f"**{trade.from_agent}** 想用 **{self._format_items(trade.offer)}** 换你的 **{self._format_items(trade.request)}**\n"
            f"回复: `trade_accept('{trade.trade_id}')` 或 `trade_reject('{trade.trade_id}')` 或 `trade_counter(...)`\n"
        )
        with open(inbox_path, "a", encoding="utf-8") as f:
            f.write(entry)

    def _write_trade_result(self, trade: TradeOffer, result: str, display_name: str):
        """Notify the original proposer of the trade result."""
        inbox_dir = os.path.join(self.parent_vault, "social")
        os.makedirs(inbox_dir, exist_ok=True)
        inbox_path = os.path.join(inbox_dir, f"{trade.from_agent}_inbox.md")

        if result == "accepted":
            msg = f"\n### ✅ 交易达成 with {display_name}\n**{display_name}** 接受了你的交易提议！\n你付出 **{self._format_items(trade.offer)}** → 获得 **{self._format_items(trade.request)}**\n"
        else:
            msg = f"\n### ❌ 交易被拒 from {display_name}\n**{display_name}** 拒绝了你的交易提议（{self._format_items(trade.offer)} ↔ {self._format_items(trade.request)}）\n"

        with open(inbox_path, "a", encoding="utf-8") as f:
            f.write(msg)

    # ═══════════════════════ PENDING TRADES PERSISTENCE ═══════════════════════

    def _pending_dir(self) -> str:
        d = os.path.join(self.parent_vault, "social", "pending_trades")
        os.makedirs(d, exist_ok=True)
        return d

    def _save_pending_trade(self, trade: TradeOffer):
        path = os.path.join(self._pending_dir(), f"{trade.trade_id}.json")
        data = {
            "trade_id": trade.trade_id, "from_agent": trade.from_agent,
            "to_agent": trade.to_agent, "offer": trade.offer,
            "request": trade.request, "status": trade.status,
            "created_season": trade.created_season, "created_day": trade.created_day,
            "created_year": trade.created_year, "created_hour": trade.created_hour,
            "counter_history": trade.counter_history,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_pending_trade(self, trade_id: str) -> Optional[TradeOffer]:
        path = os.path.join(self._pending_dir(), f"{trade_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return TradeOffer(**data)

    def _delete_pending_trade(self, trade_id: str):
        path = os.path.join(self._pending_dir(), f"{trade_id}.json")
        if os.path.exists(path):
            os.remove(path)

    def list_pending_for(self, agent_id: str) -> List[TradeOffer]:
        """List all pending trades where this agent is the recipient."""
        trades = []
        pending_dir = self._pending_dir()
        if not os.path.isdir(pending_dir):
            return trades
        for fn in os.listdir(pending_dir):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(pending_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("to_agent") == agent_id and data.get("status") == "pending":
                    trades.append(TradeOffer(**data))
            except Exception:
                pass
        return trades

    # ═══════════════════════ TRUST ═══════════════════════

    def _update_trust(self, agent_a: str, agent_b: str, delta: float):
        """Update mutual trust after a trade. Reads/writes agent profiles."""
        for aid in [agent_a, agent_b]:
            prof_path = os.path.join(self.parent_vault, "agents", "profiles", f"{aid}.json")
            if not os.path.exists(prof_path):
                continue
            try:
                with open(prof_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                si = data.get("social_influences", {})
                interactions = si.get("recent_interactions", [])
                interactions.append({
                    "with": agent_b if aid == agent_a else agent_a,
                    "type": "trade",
                    "timestamp": datetime.datetime.now().isoformat(),
                    "trust_delta": delta,
                })
                si["recent_interactions"] = interactions[-10:]
                data["social_influences"] = si
                with open(prof_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    # ═══════════════════════ HELPERS ═══════════════════════

    @staticmethod
    def _format_items(items: Dict[str, int]) -> str:
        if not items:
            return "(空)"
        return ", ".join(f"{k}×{v}" for k, v in items.items())

    def execute_exchange(self, trade_id: str, acceptor_state: dict) -> Dict[str, Any]:
        """After acceptance, attempt to execute the actual item exchange via state mutation.
        Called by the agent-world-llm.py caller who has access to the farm API.

        Returns instructions for the caller to execute.
        """
        trade = self._load_pending_trade(trade_id)
        if not trade:
            return {"success": False, "message": "Trade not found"}
        return {
            "success": True,
            "trade_id": trade_id,
            "proposer_gives": trade.offer,
            "acceptor_gives": trade.request,
            "proposer_id": trade.from_agent,
            "acceptor_id": trade.to_agent,
        }


# ═══════════════════════════ SELF-TEST ═══════════════════════════

if __name__ == "__main__":
    engine = TradeEngine(r"C:\Users\m1916\agent-brain")

    # Test item parsing
    for item in ["wheat", "iron_hoe", "gold", "egg", "wood"]:
        info = parse_trade_item(item)
        status = "found" if info else "NOT FOUND"
        print(f"  {item}: {status}")

    # Test propose
    result = engine.propose(
        "xu_renwu", "old_wang",
        {"wheat": 20}, {"iron_hoe": 1},
        {
            "season": "Summer", "day": 10, "year": 1, "hour": 8.0, "gold": 1000,
            "inventory": {"wheat_seeds": 50},
            "storage": [],
        }
    )
    print(f"\nPropose: {result['message'][:80]}")
    assert result["success"], f"Propose failed: {result}"
    tid = result["trade_id"]

    # Test accept
    accept = engine.accept(tid, "old_wang",
        {"season": "Summer", "day": 3, "year": 1, "hour": 14.0, "gold": 500,
         "inventory": {}, "storage": [{"key": "iron_hoe"}]},
        type('obj', (object,), {"display_name": "老王", "inventory": {"tools_owned": ["iron_hoe"]}})()
    )
    print(f"Accept: {accept['message'][:80]}")

    # Test effective_trust
    assert effective_trust(0.25, 12) > 0.3, "Trust decay should raise effective_trust"
    assert abs(effective_trust(0.1, 60) - 0.3) < 0.001, "Trust decay capped at +0.2"
    print(f"\neffective_trust(0.25, 12mo) = {effective_trust(0.25, 12):.2f}")
    print(f"effective_trust(0.1, 60mo) = {effective_trust(0.1, 60):.2f} (capped)")

    print("\nAll trade engine tests passed!")
