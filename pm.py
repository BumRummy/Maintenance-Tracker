"""Quarterly preventative-maintenance (PM) checklists, one per room per quarter.

Mirrors the paper "PM Quarterly Checklist": every item in a room gets a result each
quarter (OK, or Clean / Paint / Repair / Replace where those make sense), with a tag date and notes.
Records live in pm.json, one per location / room / year / quarter.
"""

import json
import threading
from datetime import date, datetime, timezone
from pathlib import Path

# What can be recorded for an item. Each item offers only the options that make sense for it.
OPTIONS = ("ok", "clean", "paint", "repair", "replace")
# Ratings saved before items had their own options (the paper legend).
LEGACY_RATINGS = {0: "paint", 1: "repair", 2: "replace", 3: "ok"}
MAX_ITEMS = 100
MAX_ITEM_LENGTH = 120

FIX = ["ok", "repair", "replace"]
CLEAN_FIX = ["ok", "clean", "repair", "replace"]
SURFACE = ["ok", "clean", "paint", "repair"]


def item(name: str, options: list[str]) -> dict:
    return {"name": name, "options": list(options)}


DEFAULT_PM_ITEMS = [
    item("CO2/Smoke Detector", FIX),
    item("Window / Screen / Lock", CLEAN_FIX),
    item("Drapes", CLEAN_FIX),
    item("Closet / Shelving / Iron", FIX),
    item("Ceiling", SURFACE),
    item("Walls", SURFACE),
    item("Headboard", CLEAN_FIX),
    item("End Tables / Handles", CLEAN_FIX),
    item("TV Console", CLEAN_FIX),
    item("Lamps / Shades / Bulbs", FIX),
    item("Switches / Cover Plates", FIX),
    item("Outlets / Cover Plates", FIX),
    item("Phone / Label", FIX),
    item("Carpet", CLEAN_FIX),
    item("Bed / Frame", FIX),
    item("Mirror", CLEAN_FIX),
    item("Pictures", CLEAN_FIX),
    item("Ceiling Vents", ["ok", "clean", "paint", "replace"]),
    item("Desk / Chair", CLEAN_FIX),
    item("Door / Latch / Peephole / Number", ["ok", "paint", "repair", "replace"]),
    item("PTAC Unit", CLEAN_FIX),
    item("Emergency Evacuation Plan", ["ok", "replace"]),
    item("Smoke Strip", FIX),
    item("Bathroom Caulk / Grout", CLEAN_FIX),
    item("Robe Hook / Linen Shelf", FIX),
    item("Drain Cover / Lever", FIX),
    item("Tub / Toilet / Sink", CLEAN_FIX),
    item("Exhaust Fan / Vent", CLEAN_FIX),
    item("Wall Vinyl", ["ok", "clean", "repair", "replace"]),
]

# Names used by the first version of the checklist, so earlier ratings still line up.
RENAMED = {
    "Window: Clean/Screen/Window Lock": "Window / Screen / Lock",
    "Drapes: Clean/Charged": "Drapes",
    "Closet: Mag Strip/shelving secure / Iron": "Closet / Shelving / Iron",
    "Ceiling: Free of marks": "Ceiling",
    "Walls: Free of marks/clean": "Walls",
    "Headboard: Secure/clean": "Headboard",
    "End Tables/Handles": "End Tables / Handles",
    "TV Console: Including shelves and drawers": "TV Console",
    "Lamps and Shades / bulbs": "Lamps / Shades / Bulbs",
    "Outlet / Cover plates": "Outlets / Cover Plates",
    "Door / Latch / Peep Hole / Sticker / number": "Door / Latch / Peephole / Number",
    "PTAC UNIT - Cleaned and working order": "PTAC Unit",
    "Smoke Strip, good working condition": "Smoke Strip",
    "Bathroom Caulk and grout": "Bathroom Caulk / Grout",
    "Door robe hook / linen shelf": "Robe Hook / Linen Shelf",
    "Drain cover/lever": "Drain Cover / Lever",
    "Porcelain/tub/toilet/sink": "Tub / Toilet / Sink",
    "Exhaust Fan pulling air vent cleaned": "Exhaust Fan / Vent",
}
_DEFAULT_OPTIONS = {i["name"]: i["options"] for i in DEFAULT_PM_ITEMS}


def normalize_rating(value) -> str | None:
    """A stored or submitted rating as one of OPTIONS ("ok"…); old 0–3 numbers are converted."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return LEGACY_RATINGS.get(value)
    value = str(value or "").strip().lower()
    return value if value in OPTIONS else None


def record_ratings(record: dict | None) -> dict[str, str]:
    """A saved checklist's ratings with current item names and option names."""
    ratings = {}
    for name, value in ((record or {}).get("ratings") or {}).items():
        rating = normalize_rating(value)
        if rating:
            ratings[RENAMED.get(name, name)] = rating
    return ratings


def items_from_storage(raw) -> list[dict]:
    """A property's checklist: [{name, options}]. Older saves were plain names."""
    if not raw:
        return [dict(i, options=list(i["options"])) for i in DEFAULT_PM_ITEMS]
    items = []
    for entry in raw:
        if isinstance(entry, str):
            name = RENAMED.get(entry, entry)
            items.append(item(name, _DEFAULT_OPTIONS.get(name, FIX)))
        else:
            items.append(item(entry["name"], entry["options"]))
    return items


def quarter_of(day: date) -> tuple[int, int]:
    """Calendar quarters: Jan–Mar is Q1 … Oct–Dec is Q4."""
    return day.year, (day.month - 1) // 3 + 1


def quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    start = date(year, 3 * quarter - 2, 1)
    end = date(year + 1, 1, 1) if quarter == 4 else date(year, 3 * quarter + 1, 1)
    return start, date.fromordinal(end.toordinal() - 1)


def clean_items(raw) -> tuple[list[dict] | None, str | None]:
    """Checklist from a supervisor: names (or {name, options}), in order. Blank and duplicate names
    are dropped; options are put in standard order and always include OK. Returns (items, error)."""
    if not isinstance(raw, list):
        return None, "Checklist items must be a list."
    items, seen = [], set()
    for entry in raw:
        if isinstance(entry, dict):
            name, options = entry.get("name"), entry.get("options")
        else:
            name, options = entry, None
        name = " ".join(str(name or "").split())
        if not name or name.casefold() in seen:
            continue
        if len(name) > MAX_ITEM_LENGTH:
            return None, f"'{name[:30]}…' is too long."
        if options is None:
            options = _DEFAULT_OPTIONS.get(name, FIX)
        if not isinstance(options, list) or any(normalize_rating(o) is None for o in options):
            return None, f"'{name}' has an option that isn't OK, Clean, Paint, Repair or Replace."
        chosen = {normalize_rating(o) for o in options} | {"ok"}
        if len(chosen) < 2:
            return None, f"'{name}' needs at least one option besides OK."
        seen.add(name.casefold())
        items.append(item(name, [o for o in OPTIONS if o in chosen]))
    if not items:
        return None, "The checklist needs at least one item."
    if len(items) > MAX_ITEMS:
        return None, f"A checklist can have at most {MAX_ITEMS} items."
    return items, None


class PMRecords:
    def __init__(self, config_dir: str):
        self.path = Path(config_dir) / "pm.json"
        self.lock = threading.Lock()

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            with self.path.open(encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return []

    def save(self, records: list[dict]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)

    def for_location(self, location: str) -> list[dict]:
        return [r for r in self.load() if r["location"] == location]

    def upsert(self, location: str, room: str, year: int, quarter: int, fields: dict) -> dict:
        with self.lock:
            records = self.load()
            record = next(
                (r for r in records if r["location"] == location and r["room"] == room
                 and r["year"] == year and r["quarter"] == quarter),
                None,
            )
            if record is None:
                record = {"location": location, "room": room, "year": year, "quarter": quarter}
                records.append(record)
            record.update(fields)
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.save(records)
            return record
