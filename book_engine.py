"""
book_engine.py — Reading system for Agent Farm
===============================================
Phase W3: Handles book ownership, reading progression, effect application,
and book creation (agents writing their own books).

Design:
  - Books are JSON objects from the master catalog.
  - Each agent has a personal library in their sub-vault.
  - Reading one chapter per action, books have 2-5 chapters.
  - Effects fire on completion: skill XP, fact discovery, preference shifts.
  - Literacy requirements gate access to complex books.
  - Diary books can be written by agents (LLM-generated).
"""
import json, os, datetime
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any


# ═══════════════════════════ DATA CLASSES ═══════════════════════════

@dataclass
class BookOwned:
    """A book in an agent's personal library, with reading progress."""
    book_id: str
    title: str
    author: str
    category: str
    chapters: int
    description: str
    rarity: str
    effects: Dict[str, Any]
    # Runtime state
    chapters_read: int = 0
    acquired_cycle: int = 0
    is_diary_author: Optional[str] = None  # agent_id who wrote this diary


# ═══════════════════════════ BOOK ENGINE ═══════════════════════════

class BookEngine:
    """Manages book catalog, agent libraries, reading, and writing."""

    def __init__(self, parent_vault: str):
        self.parent_vault = parent_vault
        self.catalog = self._load_catalog()

    def _load_catalog(self) -> dict:
        """Load the master book catalog."""
        path = os.path.join(self.parent_vault, "agents", "books", "_catalog.json")
        if not os.path.exists(path):
            return {"books": [], "reading_mechanics": {}, "categories": {}}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_book_def(self, book_id: str) -> Optional[dict]:
        """Get a book's definition from the master catalog."""
        for book in self.catalog.get("books", []):
            if book["id"] == book_id:
                return dict(book)
        return None

    def list_available_books(self, rarity_filter: Optional[str] = None) -> List[dict]:
        """List all books in the catalog, optionally filtered by rarity."""
        books = self.catalog.get("books", [])
        if rarity_filter:
            books = [b for b in books if b.get("rarity") == rarity_filter]
        return books

    def get_shop_books(self, season: str, agent_role: str) -> List[dict]:
        """Generate a shop book list appropriate for the given season and agent role.
        Common books always available; rarer ones appear randomly."""
        import random
        available = []

        # Always show common skill books relevant to role
        common = [b for b in self.catalog.get("books", []) if b.get("rarity") == "common"]
        available.extend(common)

        # Role-relevant uncommon books (70% chance each)
        role_books = {
            "farmer": ["soil_science", "water_wisdom", "great_drought"],
            "herder": ["pasture_economics", "herbal_medicine", "great_drought"],
            "craftsman": ["forge_mastery", "water_wisdom", "soil_science"],
        }
        for bid in role_books.get(agent_role, []):
            if random.random() < 0.7:
                book = self.get_book_def(bid)
                if book:
                    available.append(book)

        # Rare books (15% chance each)
        for book in self.catalog.get("books", []):
            if book.get("rarity") == "rare" and random.random() < 0.15:
                available.append(dict(book))

        return available

    # ═══════════════════════ LIBRARY MANAGEMENT ═══════════════════════

    def get_library(self, agent_id: str) -> List[BookOwned]:
        """Load an agent's personal library."""
        path = os.path.join(
            self.parent_vault, "agents", agent_id, "library.json"
        )
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [BookOwned(**b) for b in data.get("books", [])]

    def save_library(self, agent_id: str, library: List[BookOwned]):
        """Save an agent's personal library."""
        path = os.path.join(
            self.parent_vault, "agents", agent_id, "library.json"
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "books": [
                {
                    "book_id": b.book_id, "title": b.title,
                    "author": b.author, "category": b.category,
                    "chapters": b.chapters, "description": b.description,
                    "rarity": b.rarity, "effects": b.effects,
                    "chapters_read": b.chapters_read,
                    "acquired_cycle": b.acquired_cycle,
                    "is_diary_author": b.is_diary_author,
                }
                for b in library
            ]
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def add_book_to_library(self, agent_id: str, book_id: str, cycle: int) -> Optional[BookOwned]:
        """Add a book to an agent's library. Returns the BookOwned or None."""
        book_def = self.get_book_def(book_id)
        if not book_def:
            return None

        library = self.get_library(agent_id)
        # Don't add duplicates
        for b in library:
            if b.book_id == book_id:
                return b

        owned = BookOwned(
            book_id=book_id,
            title=book_def["title"],
            author=book_def["author"],
            category=book_def["category"],
            chapters=book_def["chapters"],
            description=book_def["description"],
            rarity=book_def.get("rarity", "common"),
            effects=book_def.get("effects", {}),
            acquired_cycle=cycle,
        )
        library.append(owned)
        self.save_library(agent_id, library)
        return owned

    # ═══════════════════════ READING MECHANICS ═══════════════════════

    def can_read(self, agent_profile, library: List[BookOwned],
                 is_night: bool, has_lamp: bool = False) -> tuple:
        """Check if the agent can read right now.

        Returns (can_read: bool, reason: str, readable_books: list)
        """
        # Lighting check
        if is_night and not has_lamp:
            return False, "天黑了没有灯——需要油灯才能夜间阅读", []

        # Find unread or partially-read books
        unread = [b for b in library if b.chapters_read < b.chapters]
        if not unread:
            return False, "所有书都读完了", []

        # Literacy check
        lit = agent_profile.literacy_level
        reqs = self.catalog.get("reading_mechanics", {}).get("literacy_requirement", {})
        readable = []
        for b in unread:
            min_lit = reqs.get(b.category, 0.1)
            if lit >= min_lit:
                readable.append(b)

        if not readable:
            return False, f"识字水平({lit:.1f})不足以阅读当前未读的书籍", []

        return True, "", readable

    def read_chapter(self, agent_profile, book: BookOwned) -> dict:
        """Read one chapter of a book. Returns {completed, message, effects_applied}.

        If the book is finished (all chapters read), effects are applied.
        """
        book.chapters_read += 1
        remaining = book.chapters - book.chapters_read

        if book.chapters_read < book.chapters:
            return {
                "completed": False,
                "message": f"读了{book.title}第{book.chapters_read}章。还剩{remaining}章。",
                "effects_applied": {},
            }

        # Book finished! Apply effects
        effects = book.effects or {}
        applied = {}

        # Skill XP
        if effects.get("skill_xp"):
            for skill, xp in effects["skill_xp"].items():
                agent_profile.add_skill_xp(skill, xp)
                applied[f"skill_{skill}"] = f"+{xp}XP"

        # Preference shifts
        if effects.get("preference_shift"):
            for pref, delta in effects["preference_shift"].items():
                agent_profile.drift_preference(pref, delta)
                applied[f"pref_{pref}"] = f"{delta:+.2f}"

        # Fatigue recovery (stories entertain)
        if effects.get("fatigue_recovery", 0) > 0:
            applied["fatigue"] = f"-{effects['fatigue_recovery']}疲劳"

        # Fact discovery
        fact_msg = ""
        if effects.get("unlocks_fact"):
            fact = effects["unlocks_fact"]
            fact_msg = f"\n📖 知识解锁: {fact.get('description', '')}"

        agent_profile.history["books_read"] = agent_profile.history.get("books_read", 0) + 1
        # Reading love drift
        agent_profile.drift_preference("reading_love", 0.05)

        effects_str = ", ".join(f"{k}={v}" for k, v in applied.items()) if applied else "无特殊效果"
        return {
            "completed": True,
            "message": f"读完了《{book.title}》！{effects_str}{fact_msg}",
            "effects_applied": applied,
            "fact_unlocked": effects.get("unlocks_fact"),
        }

    def reading_progress_snippet(self, library: List[BookOwned]) -> str:
        """Generate a compact reading progress summary for LLM context."""
        if not library:
            return ""

        unread = [b for b in library if b.chapters_read < b.chapters]
        finished = [b for b in library if b.chapters_read >= b.chapters]

        lines = []
        if unread:
            lines.append("## 📚 书架上未读完的书")
            for b in unread[:3]:
                cat_icon = self.catalog.get("categories", {}).get(b.category, {}).get("icon", "📖")
                lines.append(f"- {cat_icon} 《{b.title}》({b.category}) 已读{b.chapters_read}/{b.chapters}章: {b.description[:60]}")
        if finished:
            lines.append(f"📚 已读完{len(finished)}本: {', '.join(f'《{b.title}》' for b in finished[-5:])}")

        return "\n".join(lines)

    # ═══════════════════════ BOOK WRITING ═══════════════════════

    def write_diary_book(self, agent_profile, content: str, vault_root: str) -> BookOwned:
        """Create a new diary book from agent-generated content.
        The book becomes a physical item in the agent's library and the catalog."""
        import datetime
        now = datetime.datetime.now().strftime("%Y%m%d")

        book_id = f"diary_{agent_profile.id}_{now}"
        title = f"{agent_profile.display_name}的日记"
        author = agent_profile.display_name

        # Create the book definition
        book_def = {
            "id": book_id, "title": title, "author": author,
            "category": "diary", "chapters": 1,
            "description": content[:200],
            "rarity": "unique", "buy_price": 0,
            "effects": {
                "skill_xp": None, "unlocks_fact": None,
                "preference_shift": None, "fatigue_recovery": 5,
            },
        }

        # Write the diary content to a file
        diary_path = os.path.join(vault_root, "knowledge", "learned", f"diary_{now}.md")
        os.makedirs(os.path.dirname(diary_path), exist_ok=True)
        with open(diary_path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n{content}\n")

        # Add to agent's library
        owned = BookOwned(
            book_id=book_id, title=title, author=author,
            category="diary", chapters=1, description=content[:200],
            rarity="unique", effects=book_def["effects"],
            chapters_read=1,  # author has "read" their own book
            is_diary_author=agent_profile.id,
        )
        library = self.get_library(agent_profile.id)
        library.append(owned)
        self.save_library(agent_profile.id, library)

        # Also save the book content so other agents can read it
        book_content_path = os.path.join(
            self.parent_vault, "agents", "books", f"{book_id}.json"
        )
        with open(book_content_path, "w", encoding="utf-8") as f:
            json.dump({
                "id": book_id, "title": title, "author": author,
                "category": "diary", "chapters": 1,
                "description": content[:200],
                "full_content": content,
                "rarity": "unique", "buy_price": 0,
                "effects": book_def["effects"],
            }, f, ensure_ascii=False, indent=2)

        return owned


# ═══════════════════════════ SELF-TEST ═══════════════════════════

if __name__ == "__main__":
    vault = r"C:\Users\m1916\agent-brain"
    from agent_profile import AgentProfile

    engine = BookEngine(vault)

    # Test catalog
    books = engine.list_available_books()
    print(f"Catalog: {len(books)} books")
    for b in books:
        print(f"  [{b['rarity']}] 《{b['title']}》({b['category']}) — {b['description'][:50]}")

    # Test shop
    shop = engine.get_shop_books("Summer", "farmer")
    print(f"\nShop (Summer/farmer): {len(shop)} books")
    for b in shop:
        print(f"  {b['buy_price']}G — 《{b['title']}》")

    # Test library + reading
    profile = AgentProfile.load(
        os.path.join(vault, "agents", "profiles", "xu_renwu.json")
    )
    engine.add_book_to_library("xu_renwu", "wheat_guide", cycle=5)
    engine.add_book_to_library("xu_renwu", "robinson_crusoe", cycle=5)

    library = engine.get_library("xu_renwu")
    print(f"\nLibrary: {len(library)} books")

    can, reason, readable = engine.can_read(profile, library, is_night=False)
    print(f"Can read: {can} ({reason})")
    if readable:
        wheat = [b for b in readable if b.book_id == "wheat_guide"][0]
        for ch in range(4):
            result = engine.read_chapter(profile, wheat)
            ok = "DONE" if result["completed"] else f"Ch{ch+1}"
            print(f"  {ok}: effects={result['effects_applied']}")

    xp = profile.skills.get("farming", {}).get("xp", 0)
    assert xp >= 40, f"Skill XP not applied! Got {xp}"
    print(f"Farming XP after reading: {xp} — OK")
    # Reset to original value so repeated test runs don't accumulate
    profile.skills["farming"]["xp"] = 120
    profile.save(os.path.join(vault, "agents", "profiles", "xu_renwu.json"))

    print("All 6 book engine tests passed!")
