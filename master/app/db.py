"""SQLite: проекты, черновики подключения и журнал событий."""

from __future__ import annotations

import json
import os
import sqlite3
import time

PATH = os.environ.get("DB_PATH", "/data/master.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY, repo TEXT UNIQUE, app_uuid TEXT, type TEXT, branch TEXT,
  status TEXT DEFAULT 'new', last_sha TEXT, findings TEXT DEFAULT '[]', allowed_sha TEXT,
  last_deploy TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY, repo TEXT, source TEXT, branch TEXT, analysis TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, project_id INTEGER, ts REAL, level TEXT, text TEXT
);
"""


def conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
    c = sqlite3.connect(PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(projects)")}
    if "url" not in cols:  # база первой версии
        c.execute("ALTER TABLE projects ADD COLUMN url TEXT DEFAULT ''")
    return c


def one(sql: str, *args) -> dict | None:
    with conn() as c:
        row = c.execute(sql, args).fetchone()
        return dict(row) if row else None


def all_(sql: str, *args) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def run(sql: str, *args) -> int:
    with conn() as c:
        return c.execute(sql, args).lastrowid


def event(project_id: int | None, level: str, text: str):
    run("INSERT INTO events (project_id, ts, level, text) VALUES (?,?,?,?)", project_id, time.time(), level, text)


def set_findings(project_id: int, sha: str, status: str, findings: list[dict]):
    run("UPDATE projects SET last_sha=?, status=?, findings=? WHERE id=?", sha, status, json.dumps(findings, ensure_ascii=False), project_id)
