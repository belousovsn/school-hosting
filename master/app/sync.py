"""Подтягивание изменений из GitHub в копию проекта в Forgejo.

Копия в Forgejo = код из GitHub + один коммит мастера сверху (school.toml и недостающие пакеты).
При каждом обновлении копия пересобирается заново из свежего кода GitHub.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import tempfile
import tomllib
from pathlib import Path

from .analyze import analyze, needed_for_analysis, norm_pkg, parse_requirements, scope

AUTHOR = ("Мастер подключения", "master@school.local")


class SyncError(RuntimeError):
    pass


async def git(*args: str, cwd: str | None = None, timeout: int = 180, hide: str = "") -> str:
    """Запускает git и возвращает вывод. hide: строка (токен), которую нельзя показывать в ошибках."""
    env = os.environ | {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "LC_ALL": "C"}
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise SyncError(f"git {args[0]} не уложился в {timeout} секунд.")
    text = out.decode("utf-8", errors="replace")
    if hide:
        text = text.replace(hide, "***")
    if proc.returncode != 0:
        raise SyncError(f"git {args[0]}: {text.strip()[-400:]}")
    return text


async def remote_head(url: str, branch: str) -> str:
    """Последний коммит ветки в удалённом репозитории."""
    out = await git("ls-remote", url, f"refs/heads/{branch}", timeout=30)
    if not out.strip():
        raise SyncError(f"В {url} нет ветки {branch}.")
    return out.split()[0]


def build_overlay(own_contract: bool, contract_text: str, paths: list[str], read, contract_path: str = "school.toml") -> dict[str, str]:
    """Файлы мастера, которые кладутся поверх кода из GitHub.

    own_contract: в GitHub уже есть свой school.toml, тогда ничего не добавляем.
    contract_text: school.toml из прошлой копии в Forgejo.
    read(path) -> str | None: содержимое файла из GitHub.
    """
    if own_contract:
        return {}
    files = {contract_path: contract_text}
    contract = tomllib.loads(contract_text)
    if not contract.get("python"):
        return files
    base = contract.get("base_dir", "") or ""
    in_base = lambda name: posixpath.join(base, name) if base else name  # noqa: E731
    files[in_base(".python-version")] = contract["python"] + "\n"
    req_path = in_base("requirements.txt")
    current = read(req_path) or ""
    # Пакеты считаем только по файлам этой части, чтобы зависимости бота не попали к сайту.
    own_paths = [p for p in paths if not base or p.startswith(base.rstrip("/") + "/")]
    texts = {p: read(p) or "" for p in own_paths if needed_for_analysis(p)}
    missing = analyze(*scope(own_paths, texts, base), "python")["missing_packages"]
    have = parse_requirements(current)
    add = [p for p in missing if norm_pkg(p) not in have]
    if add or not current.strip():
        files[req_path] = (current.rstrip("\n") + "\n" if current.strip() else "") + "\n".join(add) + "\n"
    return files


async def sync(src_url: str, dst_url: str, branch: str, hide: str = "", contract_paths: list[str] | None = None) -> dict:
    """Собирает копию из свежего кода src и отправляет её в dst с перезаписью.
    contract_paths: описания всех частей репозитория, по умолчанию одно school.toml в корне.
    Возвращает {"src_sha", "subject", "dst_sha"}."""
    with tempfile.TemporaryDirectory(prefix="sync-") as d:
        run = lambda *a, **kw: git(*a, cwd=d, hide=hide, **kw)  # noqa: E731
        await run("init", "-q")
        await run("fetch", "-q", src_url, f"+refs/heads/{branch}:refs/remotes/src/{branch}")
        await run("fetch", "-q", dst_url, f"+refs/heads/{branch}:refs/remotes/dst/{branch}")
        await run("checkout", "-q", "-B", "work", f"refs/remotes/src/{branch}")
        src_sha = (await run("rev-parse", "HEAD")).strip()
        subject = (await run("log", "-1", "--format=%s")).strip()
        paths = [p for p in (await run("ls-files", "-z")).split("\0") if p]
        def read(path: str) -> str | None:
            f = Path(d, path)
            return f.read_text("utf-8", errors="replace") if f.is_file() and f.stat().st_size <= 300_000 else None

        overlay: dict[str, str] = {}
        for cp in contract_paths or ["school.toml"]:
            try:
                contract_text = await run("show", f"refs/remotes/dst/{branch}:{cp}")
            except SyncError:
                contract_text = ""
            own = cp in paths
            if not own and not contract_text:
                raise SyncError(f"В копии проекта нет {cp}. Удали проект и подключи заново.")
            overlay |= build_overlay(own, contract_text, paths, read, cp)
        for path, text in overlay.items():
            f = Path(d, path)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, "utf-8", newline="\n")
        await run("add", "-A")
        if (await run("status", "--porcelain")).strip():
            await run("-c", f"user.name={AUTHOR[0]}", "-c", f"user.email={AUTHOR[1]}",
                      "commit", "-q", "-m", f"Настройки школьного сервера поверх {src_sha[:8]}")
        dst_sha = (await run("rev-parse", "HEAD")).strip()
        await run("push", "-q", "--force", dst_url, f"HEAD:refs/heads/{branch}")
        return {"src_sha": src_sha, "subject": subject, "dst_sha": dst_sha}
