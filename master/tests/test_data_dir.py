"""Постоянная папка для данных (SQLite в data/ и т. п.)."""

import sqlite3

from app.analyze import analyze, contract_toml, find_data_dir, resolve

CONFIG = "from pydantic_settings import BaseSettings\n\n\nclass Settings(BaseSettings):\n    bot_token: str\n    db_path: str = \"data/mafia.db\"\n"
BOT = ("from aiogram import Bot\nfrom sqlalchemy.ext.asyncio import create_async_engine\n"
       "e = create_async_engine(f'sqlite+aiosqlite:///{path}')\nif __name__ == '__main__':\n    dp.start_polling()\n")


def test_find_data_dir():
    assert find_data_dir(["data/mafia.db"]) == "data"
    assert find_data_dir(["./storage/app.sqlite3"]) == "storage"
    assert find_data_dir(["bot.db"]) == ""  # файл в корне: папку с кодом постоянной делать нельзя
    assert find_data_dir(["/var/lib/x.db", "../x.db", ":memory:", "src/x.db"]) == ""


def test_analysis_offers_data_dir_and_contract_keeps_it():
    files = {"bot.py": BOT, "config.py": CONFIG, "requirements.txt": "aiogram\nsqlalchemy\npydantic-settings\n"}
    a = analyze(list(files), files)
    assert a["data_dir"] == "data" and any("папке data/" in w for w in a["warnings"])
    with_dir = resolve(a, {"keep_data": "on"})["contract"]
    assert with_dir["data_dir"] == "data" and 'data_dir = "data"' in contract_toml(with_dir)
    assert "data_dir" not in resolve(a, {})["contract"]  # галочку сняли: всё как раньше


def test_root_db_file_gets_advice_not_volume():
    files = {"bot.py": "import sqlite3\nfrom aiogram import Bot\nc = sqlite3.connect('bot.db')\nif __name__ == '__main__':\n    dp.start_polling()\n",
             "requirements.txt": "aiogram\n"}
    a = analyze(list(files), files)
    assert a["data_dir"] == "" and any("положи их в папку" in w for w in a["warnings"])


def test_migration_adds_data_dir(tmp_path, monkeypatch):
    from app import db
    path = tmp_path / "m.db"
    c = sqlite3.connect(path)
    c.executescript(db.PROJECTS.replace(", data_dir TEXT DEFAULT ''", ""))
    c.execute("INSERT INTO projects (repo, name, status) VALUES ('Mafia_bot', 'Mafia_bot', 'running')")
    c.commit()
    c.close()
    monkeypatch.setattr(db, "PATH", str(path))
    monkeypatch.setattr(db, "_ready", False)
    assert db.one("SELECT name, status, data_dir FROM projects") == {"name": "Mafia_bot", "status": "running", "data_dir": ""}
