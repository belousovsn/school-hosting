"""Несколько частей в одном репозитории и базы данных."""

import asyncio
import subprocess
from pathlib import Path

from app.analyze import analyze, find_parts, scope
from app.checks import check_push
from app.sync import sync

BOT = "import os\nimport asyncpg\nfrom aiogram import Bot, Dispatcher\nTOKEN = os.getenv('BOT_TOKEN')\nDB = os.getenv('DATABASE_URL')\ndp = Dispatcher()\ndp.start_polling(Bot(TOKEN))\n"
SITE = "import os\nfrom fastapi import FastAPI\nimport psycopg\napp = FastAPI()\nDB = os.environ['DATABASE_URL']\n"
REPO = {"bot/main.py": BOT, "bot/requirements.txt": "aiogram\nasyncpg\n",
        "site/app.py": SITE, "site/requirements.txt": "fastapi\n", "README.md": "", "shared/schema.sql": ""}


def test_find_parts():
    assert find_parts(list(REPO)) == ["bot", "site"]
    assert find_parts(["requirements.txt", "bot.py", "web/index.html"]) == []  # проект в корне
    assert find_parts(["app/requirements.txt", "app/main.py"]) == []  # часть одна


def test_part_analysis_and_db_detection():
    paths, files = scope(list(REPO), REPO, "site")
    assert paths == ["app.py", "requirements.txt"]
    a = analyze(paths, files)
    assert a["type"] == "python-web" and a["wants_db"]
    assert "DATABASE_URL" not in a["secrets"]  # адрес базы мастер передаёт сам
    assert "psycopg[binary]" in a["missing_packages"] and "uvicorn" in a["missing_packages"]
    b = analyze(*scope(list(REPO), REPO, "bot"))
    assert b["type"] == "telegram-bot" and b["secrets"] == ["BOT_TOKEN"] and b["wants_db"]
    assert b["missing_packages"] == []  # aiogram и asyncpg уже есть, пакеты сайта сюда не попали
    plain = analyze(["bot.py", "requirements.txt"], {"bot.py": "import os\nfrom aiogram import Bot\nos.getenv('BOT_TOKEN')\nstart_polling\n",
                                                     "requirements.txt": "aiogram\n"})
    assert not plain["wants_db"]


def test_check_push_uses_part_contract():
    tree = [{"path": p, "type": "blob", "size": 10} for p in ["site/app.py", "site/requirements.txt", "site/school.toml"]]
    contract = b'type = "python-web"\nbase_dir = "site"\nentry = "app.py"\n'
    read = lambda p: contract if p == "site/school.toml" else b""  # noqa: E731
    assert check_push(tree, read, "site/school.toml") == []
    assert check_push(tree, read)[0]["text"].startswith("Нет файла school.toml")  # старые проекты: корень


def test_webhook_skips_sibling_on_master_commit():
    from app.main import skip_for_part
    bot = {"contract_path": "bot/school.toml"}
    whole = {"contract_path": "school.toml"}
    master = {"author": {"email": "master@school.local"}, "added": ["site/school.toml", "site/.python-version"], "modified": [], "removed": []}
    student = {"author": {"email": "kid@mail.ru"}, "added": [], "modified": ["site/app.py"], "removed": []}
    assert skip_for_part(bot, {"commits": [master]})  # настройки соседней части: бот не пересобирается
    assert not skip_for_part(bot, {"commits": [student]})  # код ученика: пересобираются все части
    assert not skip_for_part(whole, {"commits": [master]})  # обычный проект ведёт себя как раньше
    assert not skip_for_part(bot, {"commits": []})


def sh(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, encoding="utf-8").stdout.strip()


def test_sync_overlays_every_part(tmp_path: Path):
    src, dst, work = tmp_path / "src", tmp_path / "dst.git", tmp_path / "work"
    src.mkdir()
    sh("init", "-q", "-b", "main", cwd=src)
    for path, text in REPO.items():
        (src / path).parent.mkdir(parents=True, exist_ok=True)
        (src / path).write_text(text, encoding="utf-8")
    sh("add", "-A", cwd=src)
    sh("commit", "-q", "-m", "first", cwd=src)
    sh("init", "-q", "--bare", "-b", "main", str(dst), cwd=tmp_path)
    sh("clone", "-q", str(src), str(work), cwd=tmp_path)
    (work / "bot/school.toml").write_text('type = "telegram-bot"\nbase_dir = "bot"\nentry = "main.py"\npython = "3.12"\n', encoding="utf-8")
    (work / "site/school.toml").write_text('type = "python-web"\nbase_dir = "site"\nentry = "app.py"\npython = "3.12"\n', encoding="utf-8")
    sh("add", "-A", cwd=work)
    sh("commit", "-q", "-m", "master", cwd=work)
    sh("push", "-q", str(dst), "HEAD:refs/heads/main", cwd=work)

    (src / "site/app.py").write_text(SITE + "import httpx\n", encoding="utf-8")
    sh("commit", "-q", "-am", "site uses httpx", cwd=src)
    asyncio.run(sync(str(src), str(dst), "main", contract_paths=["bot/school.toml", "site/school.toml"]))

    show = lambda p: sh("show", f"main:{p}", cwd=dst)  # noqa: E731
    assert "base_dir = \"bot\"" in show("bot/school.toml") and "base_dir = \"site\"" in show("site/school.toml")
    assert show("site/requirements.txt").splitlines() == ["fastapi", "psycopg[binary]", "uvicorn", "httpx"] or \
        set(show("site/requirements.txt").splitlines()) == {"fastapi", "psycopg[binary]", "uvicorn", "httpx"}
    assert show("bot/requirements.txt").splitlines() == ["aiogram", "asyncpg"]  # пакеты сайта к боту не попали
