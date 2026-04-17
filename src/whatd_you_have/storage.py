from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .config import settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    wxid TEXT PRIMARY KEY,
    nickname TEXT,
    daily_goal_kcal INTEGER,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wxid TEXT NOT NULL,
    eaten_at TEXT NOT NULL,
    local_date TEXT NOT NULL,
    meal_type TEXT,
    source TEXT NOT NULL,
    raw_input TEXT,
    summary TEXT,
    items_json TEXT NOT NULL,
    total_kcal REAL,
    protein_g REAL,
    fat_g REAL,
    carbs_g REAL
);

CREATE INDEX IF NOT EXISTS idx_meals_wxid_date ON meals(wxid, local_date);

CREATE TABLE IF NOT EXISTS nag_state (
    wxid TEXT PRIMARY KEY,
    last_nag_at TEXT,
    nag_level INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class Meal:
    id: int | None
    wxid: str
    eaten_at: datetime
    local_date: str
    meal_type: str | None
    source: str  # "image" | "text"
    raw_input: str | None
    summary: str | None
    items: list[dict[str, Any]]
    total_kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float


async def init_db() -> None:
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def upsert_user(wxid: str, nickname: str | None = None) -> None:
    now = _utcnow().isoformat()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """
            INSERT INTO users (wxid, nickname, created_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(wxid) DO UPDATE SET
                nickname = COALESCE(excluded.nickname, users.nickname),
                last_seen_at = excluded.last_seen_at
            """,
            (wxid, nickname, now, now),
        )
        await db.commit()


async def list_user_wxids() -> list[str]:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute("SELECT wxid FROM users")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def insert_meal(meal: Meal) -> int:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            """
            INSERT INTO meals (
                wxid, eaten_at, local_date, meal_type, source, raw_input,
                summary, items_json, total_kcal, protein_g, fat_g, carbs_g
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meal.wxid,
                meal.eaten_at.isoformat(),
                meal.local_date,
                meal.meal_type,
                meal.source,
                meal.raw_input,
                meal.summary,
                json.dumps(meal.items, ensure_ascii=False),
                meal.total_kcal,
                meal.protein_g,
                meal.fat_g,
                meal.carbs_g,
            ),
        )
        await db.commit()
        return cur.lastrowid or 0


async def meals_for_date(wxid: str, local_date: str) -> list[Meal]:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            """
            SELECT id, wxid, eaten_at, local_date, meal_type, source, raw_input,
                   summary, items_json, total_kcal, protein_g, fat_g, carbs_g
            FROM meals
            WHERE wxid = ? AND local_date = ?
            ORDER BY eaten_at ASC
            """,
            (wxid, local_date),
        )
        rows = await cur.fetchall()
    return [
        Meal(
            id=r[0],
            wxid=r[1],
            eaten_at=_parse(r[2]),
            local_date=r[3],
            meal_type=r[4],
            source=r[5],
            raw_input=r[6],
            summary=r[7],
            items=json.loads(r[8]),
            total_kcal=r[9] or 0.0,
            protein_g=r[10] or 0.0,
            fat_g=r[11] or 0.0,
            carbs_g=r[12] or 0.0,
        )
        for r in rows
    ]


async def last_meal_time(wxid: str) -> datetime | None:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "SELECT eaten_at FROM meals WHERE wxid = ? ORDER BY eaten_at DESC LIMIT 1",
            (wxid,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return _parse(row[0])


async def get_nag_state(wxid: str) -> tuple[datetime | None, int]:
    async with aiosqlite.connect(settings.database_path) as db:
        cur = await db.execute(
            "SELECT last_nag_at, nag_level FROM nag_state WHERE wxid = ?",
            (wxid,),
        )
        row = await cur.fetchone()
    if row is None:
        return None, 0
    last_at = _parse(row[0]) if row[0] else None
    return last_at, int(row[1] or 0)


async def set_nag_state(wxid: str, last_nag_at: datetime, level: int) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """
            INSERT INTO nag_state (wxid, last_nag_at, nag_level)
            VALUES (?, ?, ?)
            ON CONFLICT(wxid) DO UPDATE SET
                last_nag_at = excluded.last_nag_at,
                nag_level = excluded.nag_level
            """,
            (wxid, last_nag_at.isoformat(), level),
        )
        await db.commit()


async def reset_nag_state(wxid: str) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """
            INSERT INTO nag_state (wxid, last_nag_at, nag_level)
            VALUES (?, NULL, 0)
            ON CONFLICT(wxid) DO UPDATE SET nag_level = 0
            """,
            (wxid,),
        )
        await db.commit()
