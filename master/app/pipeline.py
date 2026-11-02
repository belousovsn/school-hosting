"""Сценарии мастера: подключение проекта, проверка пуша, контроль нагрузки."""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from . import db
import httpx

from .analyze import BOT_TYPES, analyze, contract_toml, needed_for_analysis, norm_pkg, parse_requirements, resolve
from .checks import MAX_SCAN, check_push, is_text, verdict
from .clients import CFG, Coolify, Docker, Forgejo, Telegram, notify
from . import sync as gitsync

fj, cf, dk = Forgejo(), Coolify(), Docker()

CPU_LIMIT_SHARE = 0.9   # считаем "упёрся в лимит" от 90% лимита
CPU_MINUTES = 10        # столько минут подряд до остановки
_cpu_strikes: dict[int, int] = {}


SYNC_MINUTES = int(os.environ.get("SYNC_MINUTES", "3"))  # как часто спрашивать GitHub о новых коммитах
_sync_locks: dict[int, asyncio.Lock] = {}
_sync_errors: dict[int, str] = {}


def is_external(url: str) -> bool:
    """Проект подключён не из нашего Forgejo, а снаружи (GitHub и т. п.)."""
    u = urlparse((url or "").strip())
    return u.scheme in ("http", "https") and bool(u.hostname) and u.hostname != urlparse(CFG["FORGEJO_URL"]).hostname


def repo_name_from_url(url: str) -> tuple[str, bool]:
    """Возвращает (имя репозитория, уже лежит в нашем Forgejo)."""
    u = urlparse(url.strip())
    parts = [p for p in u.path.split("/") if p]
    if not parts:
        raise ValueError("Не похоже на ссылку на репозиторий.")
    name = re.sub(r"\.git$", "", parts[-1])
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:60]
    ours = urlparse(CFG["FORGEJO_URL"]).hostname == u.hostname
    return name, ours


async def _read_for_analysis(repo: str, ref: str) -> tuple[list[str], dict[str, str]]:
    tree = await fj.tree(repo, ref)
    paths = [e["path"] for e in tree if e["type"] == "blob"]
    wanted = [e["path"] for e in tree if e["type"] == "blob" and e.get("size", 0) <= 300_000 and needed_for_analysis(e["path"])]
    contents = await asyncio.gather(*(fj.raw(repo, p, ref) for p in wanted))
    return paths, {p: c.decode("utf-8", errors="replace") for p, c in zip(wanted, contents)}


async def start_connect(url: str, runtime: str | None = None) -> int:
    name, ours = repo_name_from_url(url)
    if db.one("SELECT id FROM projects WHERE repo=?", name):
        raise ValueError(f"Проект {name} уже подключён.")
    if not await fj.repo(name):
        if ours:
            raise ValueError(f"В Forgejo нет репозитория {CFG['ORG']}/{name}.")
        await fj.migrate(url, name)
    branch = await fj.default_branch(name)
    sha = await fj.head(name, branch)
    paths, files = await _read_for_analysis(name, sha)
    analysis = analyze(paths, files, runtime)
    return db.run("INSERT INTO drafts (repo, source, branch, analysis, created) VALUES (?,?,?,?,?)",
                  name, url, branch, json.dumps(analysis, ensure_ascii=False), time.time())


async def finish_connect(draft_id: int, answers: dict, secrets: dict[str, str], add_missing: bool) -> tuple[int | None, list[str]]:
    d = db.one("SELECT * FROM drafts WHERE id=?", draft_id)
    a = json.loads(d["analysis"])
    # Учитель выбрал другую среду (например Node.js вместо Python): разбираем заново и показываем форму ещё раз.
    if answers.get("runtime") and answers["runtime"] != a.get("runtime"):
        sha = await fj.head(d["repo"], d["branch"])
        paths, files = await _read_for_analysis(d["repo"], sha)
        new = analyze(paths, files, answers["runtime"])
        db.run("UPDATE drafts SET analysis=? WHERE id=?", json.dumps(new, ensure_ascii=False), draft_id)
        return None, ["Тип проекта изменён. Проверь форму ещё раз и нажми «Подключить»."]
    if a["problems"]:
        return None, a["problems"]
    r = resolve(a, answers)
    if r["errors"]:
        return None, r["errors"]
    c = r["contract"]
    errors, notes = [], []
    for key in c["secrets"]:
        if not secrets.get(key):
            errors.append(f"Нужно значение для {key}.")
        elif "TOKEN" in key.upper() and c["type"] in BOT_TYPES:
            ok, msg = await Telegram.check_token(secrets[key].strip())
            (notes if ok else errors).append(msg)
    if errors:
        return None, errors

    base = c["base_dir"]
    files = {"school.toml": contract_toml(c)}
    is_python = bool(c.get("python"))
    req_path = posixpath.join(base, "requirements.txt") if base else "requirements.txt"
    if is_python:
        files[posixpath.join(base, ".python-version") if base else ".python-version"] = c["python"] + "\n"
    if is_python and r["create_requirements"]:
        files[req_path] = "\n".join(a["missing_packages"]) + "\n"
    elif is_python and add_missing and a["missing_packages"]:
        old = (await fj.raw(d["repo"], req_path, d["branch"])).decode("utf-8", errors="replace")
        have = parse_requirements(old)
        add = [p for p in a["missing_packages"] if norm_pkg(p) not in have]
        if add:
            files[req_path] = old.rstrip("\n") + "\n" + "\n".join(add) + "\n"

    # Вебхук ставим после первой сборки, иначе этот коммит собрался бы дважды.
    sha = await fj.commit_files(d["repo"], d["branch"], files, "Подготовка к запуску на школьном сервере")
    uuid, url = await cf.create_app(d["repo"], d["branch"], c)
    for key in c["secrets"]:
        await cf.set_env(uuid, key, secrets[key].strip())
    source = d["source"] if is_external(d["source"]) else ""
    pid = db.run("INSERT INTO projects (repo, app_uuid, type, branch, url, source, created) VALUES (?,?,?,?,?,?,?)",
                 d["repo"], uuid, c["type"], d["branch"], url, source, time.time())
    db.event(pid, "info", "Проект подключён." + (f" Адрес: {url}." if url else "") + (" " + " ".join(notes) if notes else ""))
    db.run("DELETE FROM drafts WHERE id=?", draft_id)
    await handle_push(pid, sha)
    await fj.set_master_hook(d["repo"])
    return pid, []


_push_locks: dict[int, asyncio.Lock] = {}


async def handle_push(project_id: int, sha: str, force: bool = False):
    # Один и тот же коммит может прийти и вебхуком, и от синхронизации с GitHub. Обрабатываем по очереди.
    async with _push_locks.setdefault(project_id, asyncio.Lock()):
        await _handle_push(project_id, sha, force)


async def _handle_push(project_id: int, sha: str, force: bool):
    p = db.one("SELECT * FROM projects WHERE id=?", project_id)
    if not p:
        return
    if not force and p["last_sha"] == sha and p["status"] in ("deploying", "running"):
        return  # этот коммит уже проверен и собирается
    tree = await fj.tree(p["repo"], sha)
    blobs = [e for e in tree if e["type"] == "blob" and e.get("size", 0) <= MAX_SCAN
             and (is_text(e["path"]) or "." not in posixpath.basename(e["path"]) or e["path"] == "school.toml")]
    contents = await asyncio.gather(*(fj.raw(p["repo"], e["path"], sha) for e in blobs))
    cache = {e["path"]: c for e, c in zip(blobs, contents)}
    findings = check_push(tree, lambda path: cache.get(path, b""))
    v = verdict(findings)
    if v == "suspicious" and p["allowed_sha"] == sha:
        v = "ok"
        db.event(project_id, "warn", f"Коммит {sha[:8]} разрешён учителем вручную.")
    if v == "ok":
        dep = await cf.deploy(p["app_uuid"], force=False)
        db.set_findings(project_id, sha, "deploying", findings)
        db.run("UPDATE projects SET last_deploy=? WHERE id=?", dep, project_id)
        db.event(project_id, "info", f"Коммит {sha[:8]} прошёл проверки, сборка запущена.")
    else:
        db.set_findings(project_id, sha, "blocked" if v == "block" else "suspicious", findings)
        text = f"{p['repo']}: коммит {sha[:8]} не собран. " + " ".join(f["text"] for f in findings if f["level"] != "warn")
        db.event(project_id, "error", text)
        await notify(text)


# Этапы сборки по строкам лога Coolify. Ищем с конца лога первую узнаваемую строку.
STAGES = [
    (("rolling update", "new container", "removing old container", "starting new application"), "Запускаю новую версию"),
    (("building docker image", "docker build", "nixpacks build", "pip install", "npm ci", "npm install"), "Собираю проект"),
    (("generating nixpacks", "nixpacks plan", "build pack"), "Готовлю сборку"),
    (("importing", "cloning", "git ", "commit sha"), "Скачиваю код"),
    (("starting deployment", "preparing container", "queued"), "Готовлю сервер"),
]
_progress: dict[int, dict] = {}


def deploy_progress(dep: dict) -> dict:
    """Этап, последняя строка лога и время начала текущей сборки."""
    try:
        logs = [l for l in json.loads(dep.get("logs") or "[]") if not l.get("hidden") and l.get("output", "").strip()]
    except (ValueError, TypeError):
        logs = []
    stage = "Жду очереди на сборку" if dep.get("status") == "queued" else "Начинаю сборку"
    for line in reversed(logs):
        low = line["output"].lower()
        match = next((name for keys, name in STAGES if any(k in low for k in keys)), None)
        if match:
            stage = match
            break
    last = logs[-1]["output"].strip().splitlines()[-1][:160] if logs else ""
    started = 0.0
    try:
        started = datetime.fromisoformat(dep["created_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, AttributeError):
        pass
    return {"stage": stage, "line": last, "started": started}


def progress(pid: int) -> dict | None:
    return _progress.get(pid)


async def refresh_status(p: dict) -> dict:
    """Подтягивает статус сборки из Coolify и проверяет, что процесс после сборки не падает."""
    pid = p["id"]
    _progress.pop(pid, None)
    if p["status"] == "deploying":
        try:
            dep = await cf.latest_deployment(p["app_uuid"])
        except Exception:
            return p
        st = (dep or {}).get("status")
        if dep and st in ("queued", "in_progress"):
            _progress[pid] = deploy_progress(dep)
        if st == "finished":
            db.run("UPDATE projects SET status='running' WHERE id=?", pid)
            db.event(pid, "info", "Сборка прошла, проект запущен.")
        elif st in ("failed", "cancelled-by-user"):
            db.run("UPDATE projects SET status='failed' WHERE id=?", pid)
            db.event(pid, "error", "Сборка упала, подробности в логе сборки.")
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    if p["status"] in ("running", "web_down") and p.get("url"):
        ok = await site_responds(p["url"])
        if not ok and p["status"] == "running":
            db.run("UPDATE projects SET status='web_down' WHERE id=?", pid)
            db.event(pid, "error", f"Сайт {p['url']} не открывается. Если сборка только что закончилась, обнови страницу через минуту.")
        elif ok and p["status"] == "web_down":
            db.run("UPDATE projects SET status='running' WHERE id=?", pid)
            db.event(pid, "info", f"Сайт {p['url']} открывается.")
        p = db.one("SELECT * FROM projects WHERE id=?", pid)
    if p["status"] == "running" and dk.enabled:
        try:
            cont = await dk.container_for(p["app_uuid"])
            stt = await dk.state(cont["Id"]) if cont else {"status": "missing", "restarts": 0}
        except Exception:
            return p
        if stt["status"] != "running" or stt["restarts"] > 0:
            tail = (await dk.logs(cont["Id"], 15)).strip()[-1200:] if cont else ""
            db.run("UPDATE projects SET status='crashing' WHERE id=?", pid)
            db.event(pid, "error", "Программа падает после запуска. Последние строки лога: " + (tail or "(пусто)"))
            p = db.one("SELECT * FROM projects WHERE id=?", pid)
    return p


_site_cache: dict[str, tuple[float, bool]] = {}


async def site_responds(url: str) -> bool:
    """Сайт считается рабочим, если отвечает без ошибки сервера и это не заглушка прокси.
    Ответ кешируется на 20 секунд: страницы панели опрашивают статус часто."""
    cached = _site_cache.get(url)
    if cached and time.time() - cached[0] < 20:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
            r = await c.get(url)
        ok = r.status_code < 500 and "no available server" not in r.text.lower()
    except httpx.HTTPError:
        ok = False
    _site_cache[url] = (time.time(), ok)
    return ok


async def delete_project(pid: int):
    """Убирает проект с сервера. Репозиторий в Forgejo остаётся, из него можно подключить проект заново."""
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    await cf.delete(p["app_uuid"])
    try:
        await fj.remove_hooks(p["repo"])
    except Exception:
        pass  # репозитория уже может не быть
    db.run("DELETE FROM events WHERE project_id=?", pid)
    db.run("DELETE FROM projects WHERE id=?", pid)
    _progress.pop(pid, None)
    _cpu_strikes.pop(pid, None)


async def project_source(p: dict) -> str:
    """Откуда подключён проект. У старых проектов адрес берём из Forgejo и запоминаем."""
    if p.get("source"):
        return p["source"]
    repo = await fj.repo(p["repo"]) or {}
    src = repo.get("original_url") or ""
    if is_external(src):
        db.run("UPDATE projects SET source=? WHERE id=?", src, p["id"])
        return src
    return ""


async def check_github(pid: int) -> str:
    """Забирает новые коммиты из GitHub, если они есть. Возвращает текст для учителя."""
    async with _sync_locks.setdefault(pid, asyncio.Lock()):
        p = db.one("SELECT * FROM projects WHERE id=?", pid)
        if not p:
            return ""
        src = await project_source(p)
        if not src:
            return "Проект подключён не из GitHub, обновлять нечего."
        head = await gitsync.remote_head(src, p["branch"])
        if head == p["source_sha"]:
            return "Нового в GitHub нет."
        if not p["source_sha"] and await fj.has_commit(p["repo"], head):
            # Проект подключён до появления синхронизации: этот коммит уже в копии.
            db.run("UPDATE projects SET source_sha=? WHERE id=?", head, pid)
            return "Нового в GitHub нет."
        res = await gitsync.sync(src, fj.push_url(p["repo"]), p["branch"], hide=CFG["FORGEJO_TOKEN"])
        db.run("UPDATE projects SET source_sha=? WHERE id=?", res["src_sha"], pid)
        db.event(pid, "info", f"Из GitHub пришёл коммит {res['src_sha'][:8]} «{res['subject'][:120]}».")
    await handle_push(pid, res["dst_sha"])
    return f"Забрал из GitHub коммит {res['src_sha'][:8]} «{res['subject'][:80]}»."


async def github_watcher():
    """Раз в несколько минут проверяет GitHub у всех проектов, кроме остановленных."""
    while True:
        await asyncio.sleep(SYNC_MINUTES * 60)
        for p in db.all_("SELECT * FROM projects WHERE status != 'stopped'"):
            try:
                await check_github(p["id"])
                if _sync_errors.pop(p["id"], None):
                    db.event(p["id"], "info", "Связь с GitHub восстановилась.")
            except Exception as e:  # одна ошибка не должна останавливать проверку остальных
                msg = f"Не получилось забрать код из GitHub: {e}"
                if _sync_errors.get(p["id"]) != msg:  # одну и ту же ошибку пишем в журнал один раз
                    _sync_errors[p["id"]] = msg
                    db.event(p["id"], "warn", msg)


async def cpu_monitor():
    """Раз в минуту смотрит загрузку. Проект, который 10 минут подряд упирается в лимит, останавливается."""
    if not dk.enabled:
        return
    while True:
        for p in db.all_("SELECT * FROM projects WHERE status IN ('running','deploying')"):
            try:
                cont = await dk.container_for(p["app_uuid"])
                if not cont:
                    continue
                used, limit = await dk.cpu_usage(cont["Id"])
                if used >= CPU_LIMIT_SHARE * limit:
                    _cpu_strikes[p["id"]] = _cpu_strikes.get(p["id"], 0) + 1
                else:
                    _cpu_strikes.pop(p["id"], None)
                if _cpu_strikes.get(p["id"], 0) >= CPU_MINUTES:
                    restarts = (await dk.state(cont["Id"]))["restarts"]
                    await cf.stop(p["app_uuid"])
                    db.run("UPDATE projects SET status='stopped' WHERE id=?", p["id"])
                    if restarts:
                        text = (f"{p['repo']} остановлен: программа {CPU_MINUTES} минут падала и перезапускалась по кругу "
                                f"({restarts} перезапусков). Причина обычно в логе: неверный токен или ошибка в коде.")
                    else:
                        text = f"{p['repo']} остановлен: {CPU_MINUTES} минут подряд упирался в лимит процессора. Так ведут себя майнеры и зависшие циклы."
                    db.event(p["id"], "error", text)
                    await notify(text)
                    _cpu_strikes.pop(p["id"], None)
            except Exception as e:  # монитор не должен падать из-за одного проекта
                db.event(p["id"], "warn", f"Монитор не смог проверить проект: {e}")
        await asyncio.sleep(60)
