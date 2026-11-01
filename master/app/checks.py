"""Проверки перед каждой сборкой. Только стандартная библиотека.

Уровни:
- block: сборка запрещена, учитель обойти не может (токен в коде, размер, контракт).
- suspicious: сборка остановлена, учитель может разрешить вручную.
"""

from __future__ import annotations

import posixpath
import re
import tomllib

from .analyze import TG_TOKEN_RE

MAX_FILE = 20 * 1024 * 1024
MAX_REPO = 200 * 1024 * 1024
MAX_SCAN = 1024 * 1024  # текстовые файлы больше мегабайта не читаем

TEXT_EXT = (".py", ".txt", ".toml", ".json", ".yml", ".yaml", ".sh", ".cfg", ".ini", ".env", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".html")
TEXT_NAMES = ("Dockerfile", "Procfile", "Makefile")
BINARY_EXT = (".exe", ".so", ".elf", ".bin", ".dll", ".dylib")

SUSPICIOUS = [
    ("майнер", re.compile(r"xmrig|stratum\+(?:tcp|ssl)|cpuminer|\bminerd\b|cryptonight|randomx|nicehash|moneroocean|nanopool|2miners|f2pool|hashvault", re.I)),
    ("сканер сети", re.compile(r"\bmasscan\b|\bzmap\b|python-nmap|\bnmap\b", re.I)),
    ("скачивание и запуск программ", re.compile(r"(?:curl|wget)[^\n|]*\|\s*(?:ba)?sh|chmod\s+\+x|os\.chmod\([^)]*0o?7[0-7]{2}", re.I)),
]


def is_text(path: str) -> bool:
    return path.endswith(TEXT_EXT) or posixpath.basename(path) in TEXT_NAMES


def check_push(tree: list[dict], read) -> list[dict]:
    """tree: [{path, type, size}], read(path) -> bytes. Возвращает список находок."""
    findings: list[dict] = []
    blobs = [e for e in tree if e.get("type") == "blob"]
    names = {e["path"] for e in blobs}

    def add(level, code, text):
        findings.append({"level": level, "code": code, "text": text})

    # Размер.
    total = sum(e.get("size", 0) for e in blobs)
    for e in blobs:
        if e.get("size", 0) > MAX_FILE:
            add("block", "size", f"Файл {e['path']} весит {e['size'] // 1024 // 1024} МБ, лимит 20 МБ.")
    if total > MAX_REPO:
        add("block", "size", f"Репозиторий весит {total // 1024 // 1024} МБ, лимит 200 МБ.")

    # Контракт.
    contract = None
    if "school.toml" not in names:
        add("block", "contract", "Нет файла school.toml. Подключи проект через мастер.")
    else:
        try:
            contract = tomllib.loads(read("school.toml").decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            add("block", "contract", f"school.toml не читается: {e}")
    if contract:
        base = contract.get("base_dir", "") or ""
        kind = contract.get("type", "")
        in_base = lambda name: posixpath.join(base, name) if base else name  # noqa: E731
        if contract.get("entry"):
            entry = posixpath.normpath(in_base(contract["entry"]))
            if entry not in names:
                add("block", "contract", f"Файл запуска {entry} из school.toml не найден.")
        required = {"static": "index.html"}.get(kind) or ("package.json" if kind.startswith("node") else "requirements.txt")
        if in_base(required) not in names:
            add("block", "contract", f"Нет файла {in_base(required)}.")
        if any(p.startswith(in_base("node_modules/")) for p in names):
            add("warn", "size", "В репозиторий попала папка node_modules. Её стоит удалить и добавить в .gitignore.")

    # Содержимое.
    for e in blobs:
        path, size = e["path"], e.get("size", 0)
        if "node_modules/" in path:  # чужие библиотеки не сканируем, о самой папке уже предупредили
            continue
        if path.endswith(BINARY_EXT):
            add("suspicious", "binary", f"В репозитории исполняемый файл {path}.")
            continue
        if size > MAX_SCAN:
            continue
        if not is_text(path):
            if "." not in posixpath.basename(path) and size > 0:
                head = read(path)[:4]
                if head == b"\x7fELF":
                    add("suspicious", "binary", f"В репозитории исполняемый файл {path}.")
            continue
        text = read(path).decode("utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if TG_TOKEN_RE.search(line):
                add("block", "token", f"Токен бота в {path}:{i}. Сделай /revoke в @BotFather, токен храни только в настройках проекта.")
                break
        for label, rx in SUSPICIOUS:
            m = rx.search(text)
            if m:
                line_no = text.count("\n", 0, m.start()) + 1
                add("suspicious", "forbidden", f"Похоже на {label}: {path}:{line_no} ({m.group(0)!r}).")
    return findings


def verdict(findings: list[dict]) -> str:
    levels = {f["level"] for f in findings}
    if "block" in levels:
        return "block"
    if "suspicious" in levels:
        return "suspicious"
    return "ok"
