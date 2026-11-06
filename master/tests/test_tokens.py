"""Токены ботов в любых текстовых файлах, а не только в коде (случай Mafia_bot: токен в run_bot.bat)."""

import asyncio

from app.checks import check_push, verdict

TOKEN = "1234567890:" + "A" * 35
CONTRACT = b'type = "telegram-bot"\nentry = "bot.py"\n'


def run(extra: dict[str, bytes]):
    files = {"school.toml": CONTRACT, "bot.py": b"import os\nos.getenv('BOT_TOKEN')\n", "requirements.txt": b"aiogram\n"} | extra
    tree = [{"path": p, "type": "blob", "size": len(c)} for p, c in files.items()]
    return check_push(tree, lambda p: files[p])


def test_token_in_bat_and_other_text_files_blocks():
    for name in ("run_bot.bat", "start.cmd", "notes.md", "config/secrets", "deploy.ps1"):
        f = run({name: f'set "BOT_TOKEN={TOKEN}"\n'.encode()})
        tok = [x for x in f if x["code"] == "token"]
        assert tok and tok[0]["_token"] == TOKEN and f"{name}:1" in tok[0]["text"], name
        assert verdict(f) == "block"


def test_media_and_binary_are_not_read_as_text():
    assert verdict(run({"logo.png": TOKEN.encode(), "data.bin.gz": TOKEN.encode()})) == "ok"
    assert verdict(run({"blob": b"\0\0" + TOKEN.encode()})) == "ok"


def test_revoked_token_does_not_block(monkeypatch):
    from app import pipeline

    async def dead(token):
        return False

    async def alive(token):
        return True

    f = run({"run_bot.bat": f"set BOT_TOKEN={TOKEN}\n".encode()})
    monkeypatch.setattr(pipeline.Telegram, "token_alive", staticmethod(dead))
    asyncio.run(pipeline._soften_revoked_tokens(f))
    assert verdict(f) == "ok" and "уже отозван" in f[0]["text"] and "_token" not in f[0]

    f = run({"run_bot.bat": f"set BOT_TOKEN={TOKEN}\n".encode()})
    monkeypatch.setattr(pipeline.Telegram, "token_alive", staticmethod(alive))
    asyncio.run(pipeline._soften_revoked_tokens(f))
    assert verdict(f) == "block" and "_token" not in f[0]  # сам токен в базу мастера не попадает
