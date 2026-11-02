"""SQLite: проекты, базы данных, черновики подключения и журнал событий."""

from __future__ import annotations

import json
import os
import sqlite3
import time

PATH = os.environ.get("DB_PATH", "/data/master.db")

# repo: имя репозитория в Forgejo. Из одного репозитория может быть несколько частей (бот и сайт),
# поэтому уникально name: имя проекта, приложения в Coolify и поддомена.
PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY, repo TEXT, name TEXT UNIQUE, app_uuid TEXT, type TEXT, branch TEXT,
  status TEXT DEFAULT 'new', last_sha TEXT, findings TEXT DEFAULT '[]', allowed_sha TEXT,
  last_deploy TEXT, created REAL, url TEXT DEFAULT '', source TEXT DEFAULT '', source_sha TEXT DEFAULT '',
  contract_path TEXT DEFAULT 'school.toml', db_id INTEGER
);
"""
SCHEMA = PROJECTS + """
CREATE TABLE IF NOT EXISTS databases (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, uuid TEXT, url TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY, repo TEXT, source TEXT, branch TEXT, analysis TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, project_id INTEGER, ts REAL, level TEXT, text TEXT
);
"""
_ready = False


def migrate(c: sqlite3.Connection):
    """Переводит базу старых версий на текущую схему, не теряя проекты и журнал."""
    old = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'").fetchone()
    if old and "name TEXT UNIQUE" not in old[0]:
        cols = [r[1] for r in c.execute("PRAGMA table_info(projects)")]
        c.execute("ALTER TABLE projects RENAME TO projects_old")
        c.executescript(PROJECTS)
        new_cols = {r[1] for r in c.execute("PRAGMA table_info(projects)")}
        keep = [col for col in cols if col in new_cols]
        names = ", ".join(keep)
        c.execute(f"INSERT INTO projects ({names}, name) SELECT {names}, repo FROM projects_old")
        c.execute("DROP TABLE projects_old")
    c.executescript(SCHEMA)


def conn() -> sqlite3.Connection:
    global _ready
    os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
    c = sqlite3.connect(PATH)
    c.row_factory = sqlite3.Row
    if not _ready:
        with c:
            migrate(c)
        _ready = True
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
