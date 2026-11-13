"""Quarterly preventative-maintenance (PM) checklists, one per room per quarter.

Mirrors the paper "PM Quarterly Checklist": every item in a room is rated each
quarter (0 Paint, 1 Repair, 2 Replace, 3 OK), with a tag date and notes.
Records live in pm.json, one per location / room / year / quarter.
"""

import json
import threading
from datetime import date, datetime, timezone
from pathlib import Path

RATINGS = {0: "Paint", 1: "Repair", 2: "Replace", 3: "OK"}
MAX_ITEMS = 100
MAX_ITEM_LENGTH = 120

DEFAULT_PM_ITEMS = [
    "CO2/Smoke Detector",
    "Window: Clean/Screen/Window Lock",
    "Drapes: Clean/Charged",
    "Closet: Mag Strip/shelving secure / Iron",
    "Ceiling: Free of marks",
    "Walls: Free of marks/clean",
    "Headboard: Secure/clean",
    "End Tables/Handles",
    "TV Console: Including shelves and drawers",
    "Lamps and Shades / bulbs",
    "Switches / Cover Plates",
    "Outlet / Cover plates",
    "Phone / Label",
    "Carpet",
    "Bed / Frame",
    "Mirror",
    "Pictures",
    "Ceiling Vents",
    "Desk / Chair",
    "Door / Latch / Peep Hole / Sticker / number",
    "PTAC UNIT - Cleaned and working order",
    "Emergency Evacuation Plan",
    "Smoke Strip, good working condition",
    "Bathroom Caulk and grout",
    "Door robe hook / linen shelf",
    "Drain cover/lever",
    "Porcelain/tub/toilet/sink",
    "Exhaust Fan pulling air vent cleaned",
    "Wall Vinyl",
]


def quarter_of(day: date) -> tuple[int, int]:
    """Calendar quarters: Jan–Mar is Q1 … Oct–Dec is Q4."""
    return day.year, (day.month - 1) // 3 + 1


def quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    start = date(year, 3 * quarter - 2, 1)
    end = date(year + 1, 1, 1) if quarter == 4 else date(year, 3 * quarter + 1, 1)
    return start, date.fromordinal(end.toordinal() - 1)


def clean_items(raw) -> tuple[list[str] | None, str | None]:
    """Trim, drop blanks and duplicates (keeping order); returns (items, error)."""
    if not isinstance(raw, list):
        return None, "Checklist items must be a list."
    items, seen = [], set()
    for value in raw:
        item = " ".join(str(value or "").split())
        if not item or item.casefold() in seen:
            continue
        if len(item) > MAX_ITEM_LENGTH:
            return None, f"'{item[:30]}…' is too long."
        seen.add(item.casefold())
        items.append(item)
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
