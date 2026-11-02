"""Синхронизация с GitHub на локальных репозиториях: src играет роль GitHub, dst роль Forgejo."""

import asyncio
import subprocess
from pathlib import Path

from app.sync import build_overlay, remote_head, sync

CONTRACT = 'type = "telegram-bot"\nbase_dir = ""\nentry = "bot.py"\npython = "3.12"\nsecrets = ["BOT_TOKEN"]\n'


def sh(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, encoding="utf-8").stdout.strip()


def write(root: Path, path: str, text: str):
    f = root / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


def make_repos(tmp_path: Path):
    src, dst, work = tmp_path / "src", tmp_path / "dst.git", tmp_path / "work"
    src.mkdir()
    sh("init", "-q", "-b", "main", cwd=src)
    write(src, "bot.py", "import os\nfrom aiogram import Bot\n")
    write(src, "requirements.txt", "aiogram==3.4\n")
    sh("add", "-A", cwd=src)
    sh("commit", "-q", "-m", "first", cwd=src)
    sh("init", "-q", "--bare", "-b", "main", str(dst), cwd=tmp_path)
    sh("clone", "-q", str(src), str(work), cwd=tmp_path)
    write(work, "school.toml", CONTRACT)  # коммит мастера при подключении
    sh("add", "-A", cwd=work)
    sh("commit", "-q", "-m", "master", cwd=work)
    sh("push", "-q", str(dst), "HEAD:refs/heads/main", cwd=work)
    return src, dst


def show(dst: Path, path: str) -> str:
    return sh("show", f"main:{path}", cwd=dst)


def test_sync_brings_new_code_and_keeps_master_files(tmp_path):
    src, dst = make_repos(tmp_path)
    write(src, "bot.py", "import os\nimport requests\nfrom aiogram import Bot\n")
    write(src, "handlers/game.py", "ROLES = ['мафия']\n")
    sh("add", "-A", cwd=src)
    sh("commit", "-q", "-m", "Новые роли", cwd=src)
    head = sh("rev-parse", "HEAD", cwd=src)

    assert asyncio.run(remote_head(str(src), "main")) == head
    res = asyncio.run(sync(str(src), str(dst), "main"))

    assert res["src_sha"] == head and res["subject"] == "Новые роли"
    assert sh("rev-parse", "main", cwd=dst) == res["dst_sha"]
    assert sh("rev-parse", "main~1", cwd=dst) == head  # копия = код GitHub + один коммит сверху
    assert "import requests" in show(dst, "bot.py")
    assert "мафия" in show(dst, "handlers/game.py")
    assert show(dst, "school.toml") == CONTRACT.strip()
    assert show(dst, ".python-version") == "3.12"
    assert show(dst, "requirements.txt").splitlines() == ["aiogram==3.4", "requests"]


def test_own_school_toml_in_github_wins(tmp_path):
    src, dst = make_repos(tmp_path)
    own = CONTRACT.replace("bot.py", "main.py")
    write(src, "school.toml", own)
    sh("add", "-A", cwd=src)
    sh("commit", "-q", "-m", "свой контракт", cwd=src)
    res = asyncio.run(sync(str(src), str(dst), "main"))
    assert res["dst_sha"] == res["src_sha"]  # мастер ничего не добавил
    assert show(dst, "school.toml") == own.strip()


def test_overlay_creates_requirements_when_missing():
    files = {"app.py": "from flask import Flask\napp = Flask(__name__)\n"}
    contract = 'type = "python-web"\nbase_dir = ""\nentry = "app.py"\npython = "3.12"\n'
    out = build_overlay(False, contract, list(files), files.get)
    assert out["requirements.txt"] == "flask\n"
    assert build_overlay(False, 'type = "static"\nbase_dir = "site"\n', ["site/index.html"], files.get) == \
        {"school.toml": 'type = "static"\nbase_dir = "site"\n'}
