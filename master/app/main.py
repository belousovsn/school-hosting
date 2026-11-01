"""Веб-интерфейс мастера подключения и приём вебхуков Forgejo."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from html import escape

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import db, pipeline
from .analyze import BOT_TYPES, TYPES, WEB_TYPES
from .clients import CFG, ApiError, Telegram, site_url

USER = os.environ.get("MASTER_USER", "teacher")
PASSWORD = os.environ.get("MASTER_PASSWORD", "")
security = HTTPBasic()


def auth(cred: HTTPBasicCredentials = Depends(security)):
    ok = PASSWORD and secrets.compare_digest(cred.username, USER) and secrets.compare_digest(cred.password, PASSWORD)
    if not ok:
        raise HTTPException(401, "Нужен вход", headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(pipeline.cpu_monitor())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

STATUS = {
    "new": ("Новый", "muted"), "deploying": ("Собирается", "info"), "running": ("Работает", "ok"),
    "failed": ("Сборка упала", "bad"), "blocked": ("Заблокирован", "bad"),
    "suspicious": ("Ждёт решения учителя", "warn"), "stopped": ("Остановлен", "muted"),
    "crashing": ("Падает после запуска", "bad"), "web_down": ("Сайт не открывается", "bad"),
}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2433;--muted:#6b7280;--line:#e5e7eb;--ok:#15803d;--bad:#b91c1c;--warn:#b45309;--info:#1d4ed8;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1a1d24;--ink:#e6e8ec;--muted:#9aa1ad;--line:#2b2f38;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--info:#93c5fd;--accent:#60a5fa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 16px}h2{font-size:17px;margin:24px 0 8px}
a{color:var(--accent)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:12px}
table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.info{color:var(--info)}.muted{color:var(--muted)}
input[type=text],input[type=password],input[type=url]{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--accent)}form.inline{display:inline}
ul{margin:4px 0;padding-left:20px}code{font-size:13px}nav{margin-bottom:16px}label{display:block;margin:4px 0}
"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title><style>{CSS}</style></head>
<body><main><nav><a href="/">Проекты</a> · <a href="/connect">Подключить проект</a></nav><h1>{escape(title)}</h1>{body}</main></body></html>""")


def status_badge(s: str) -> str:
    label, cls = STATUS.get(s, (s, "muted"))
    return f'<span class="{cls}">{label}</span>'


def items(lst, cls="") -> str:
    return "<ul>" + "".join(f'<li class="{cls}">{escape(x)}</li>' for x in lst) + "</ul>" if lst else ""


@app.get("/", dependencies=[Depends(auth)])
async def index():
    rows = ""
    for p in db.all_("SELECT * FROM projects ORDER BY id DESC"):
        p = await pipeline.refresh_status(p)
        link = f'<a href="{escape(p["url"])}" target="_blank">открыть</a>' if p.get("url") else ""
        kind = TYPES.get(p["type"], p["type"] or "")
        rows += f'<tr><td><a href="/projects/{p["id"]}">{escape(p["repo"])}</a></td><td>{escape(kind)}</td><td>{status_badge(p["status"])}</td><td>{link}</td></tr>'
    body = f'<div class="card"><table><tr><th>Проект</th><th>Тип</th><th>Состояние</th><th>Сайт</th></tr>{rows}</table></div>' if rows else \
        '<div class="card">Пока ни одного проекта. <a href="/connect">Подключить первый</a>.</div>'
    return page("Проекты", body)


@app.get("/connect", dependencies=[Depends(auth)])
async def connect_form(error: str = ""):
    err = f'<p class="bad">{escape(error)}</p>' if error else ""
    return page("Подключить проект", f"""<div class="card">{err}
<form method="post" action="/connect"><label>Ссылка на репозиторий (GitHub или наш Forgejo)</label>
<input type="url" name="url" required placeholder="https://github.com/user/my-project">
<p class="muted">Подходят Telegram-боты и сайты на Python (Flask, FastAPI, Django), сайты и боты на Node.js, статические сайты на HTML.</p>
<p class="muted">Мастер скопирует репозиторий в Forgejo, разберёт код и спросит только то, что не смог понять сам.</p>
<button>Разобрать проект</button></form></div>""")


@app.post("/connect", dependencies=[Depends(auth)])
async def connect(url: str = Form(...)):
    try:
        did = await pipeline.start_connect(url)
    except (ValueError, ApiError) as e:
        return await connect_form(error=str(e))
    return RedirectResponse(f"/drafts/{did}", 303)


def draft_body(d: dict, errors: list[str] | None = None) -> str:
    a = json.loads(d["analysis"])
    out = f'<div class="card"><b>{escape(d["repo"])}</b> <span class="muted">из {escape(d["source"])}</span><br>'
    out += f'Тип: {escape(TYPES.get(a["type"], a["type"]))}' + (f' ({escape(a["framework"])})' if a.get("framework") else "")
    if a["type"] in WEB_TYPES:
        out += f'<br>Адрес будет: <code>{escape(site_url(d["repo"]))}</code>'
    out += "</div>"
    if errors:
        out += f'<div class="card"><b class="bad">Не получилось</b>{items(errors, "bad")}</div>'
    if a["problems"]:
        out += f'<div class="card"><b class="bad">Сначала нужно исправить в коде</b>{items(a["problems"], "bad")}' \
               f'<form method="post" action="/drafts/{d["id"]}/recheck"><button class="ghost">Я исправил, проверить снова</button></form></div>'
        return out
    out += f'<form method="post" action="/drafts/{d["id"]}"><div class="card">'
    for q in a["questions"]:
        out += f"<p><b>{escape(q['text'])}</b></p>" + "".join(
            f'<label><input type="radio" name="q_{q["id"]}" value="{escape(o)}" {"checked" if i == 0 else ""}> {escape(q.get("labels", {}).get(o, o))}</label>'
            for i, o in enumerate(q["options"]))
    if not any(q["id"] == "entry" for q in a["questions"]) and a["entry_candidates"]:
        out += f'<p>Главный файл: <code>{escape(a["entry_candidates"][0])}</code></p>'
    elif a.get("has_start_script"):
        out += '<p>Запуск: <code>npm start</code> из package.json</p>'
    if a["missing_packages"] and a["type"] in ("telegram-bot", "python-web", "python-app"):
        out += f'<p><label><input type="checkbox" name="add_missing" checked> Добавить в requirements.txt: <code>{escape(", ".join(a["missing_packages"]))}</code></label></p>'
    if a["warnings"]:
        out += f'<p class="warn">Обрати внимание:</p>{items(a["warnings"], "warn")}'
    out += "</div>"
    if a["secrets"]:
        tg_note = " Токен бота проверю в Telegram." if a["type"] in BOT_TYPES else ""
        out += f'<div class="card"><b>Секреты</b><p class="muted">Хранятся только на сервере, в репозиторий не попадают.{tg_note}</p>'
        out += "".join(f'<label>{escape(s)}<input type="password" name="s_{escape(s)}" autocomplete="off" required></label>' for s in a["secrets"])
        out += "</div>"
    out += '<button>Подключить и запустить</button></form>'
    return out


@app.get("/drafts/{did}", dependencies=[Depends(auth)])
async def draft(did: int):
    d = db.one("SELECT * FROM drafts WHERE id=?", did)
    if not d:
        raise HTTPException(404)
    return page("Подключение проекта", draft_body(d))


@app.post("/drafts/{did}/recheck", dependencies=[Depends(auth)])
async def draft_recheck(did: int):
    d = db.one("SELECT * FROM drafts WHERE id=?", did)
    db.run("DELETE FROM drafts WHERE id=?", did)
    new = await pipeline.start_connect(d["source"])
    return RedirectResponse(f"/drafts/{new}", 303)


@app.post("/drafts/{did}", dependencies=[Depends(auth)])
async def draft_submit(did: int, request: Request):
    form = await request.form()
    answers = {k[2:]: v for k, v in form.items() if k.startswith("q_")}
    secrets_ = {k[2:]: v for k, v in form.items() if k.startswith("s_")}
    try:
        pid, errors = await pipeline.finish_connect(did, answers, secrets_, "add_missing" in form)
    except ApiError as e:
        pid, errors = None, [str(e)]
    if pid:
        return RedirectResponse(f"/projects/{pid}", 303)
    d = db.one("SELECT * FROM drafts WHERE id=?", did)
    return page("Подключение проекта", draft_body(d, errors))


@app.get("/projects/{pid}", dependencies=[Depends(auth)])
async def project(pid: int):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    if not p:
        raise HTTPException(404)
    p = await pipeline.refresh_status(p)
    findings = json.loads(p["findings"] or "[]")
    body = f'<div class="card">Состояние: <b>{status_badge(p["status"])}</b><br>Репозиторий: <a href="{escape(CFG["FORGEJO_URL"])}/{CFG["ORG"]}/{escape(p["repo"])}">{CFG["ORG"]}/{escape(p["repo"])}</a>'
    body += f'<br>Тип: {escape(TYPES.get(p["type"], p["type"] or ""))}'
    if p.get("url"):
        body += f'<br>Сайт: <a href="{escape(p["url"])}" target="_blank">{escape(p["url"])}</a>'
    body += f'<br>Последний коммит: <code>{escape((p["last_sha"] or "")[:8])}</code></div>'
    if findings:
        body += '<div class="card"><b>Проверка последнего коммита</b><ul>' + "".join(
            f'<li class="{"bad" if f["level"] == "block" else "warn"}">{escape(f["text"])}</li>' for f in findings) + "</ul>"
        if p["status"] == "suspicious":
            body += f'<form method="post" action="/projects/{pid}/allow"><button class="ghost">Я посмотрел код, разрешить этот коммит</button></form>'
        body += "</div>"
    body += f"""<div class="card">
<form class="inline" method="post" action="/projects/{pid}/redeploy"><button>Проверить и пересобрать</button></form>
<form class="inline" method="post" action="/projects/{pid}/stop"><button class="ghost">Остановить</button></form>
<form class="inline" method="post" action="/projects/{pid}/start"><button class="ghost">Запустить</button></form></div>
<div class="card"><b>Обновить секрет</b><form method="post" action="/projects/{pid}/secret">
<label>Имя<input type="text" name="key" value="BOT_TOKEN"></label><label>Значение<input type="password" name="value" autocomplete="off"></label>
<button class="ghost">Сохранить и перезапустить</button></form></div>"""
    ev = db.all_("SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT 30", pid)
    body += "<h2>Журнал</h2><div class='card'><table>" + "".join(
        f'<tr><td class="muted">{time.strftime("%d.%m %H:%M", time.localtime(e["ts"]))}</td><td class="{ {"error": "bad", "warn": "warn"}.get(e["level"], "")}">{escape(e["text"])}</td></tr>'
        for e in ev) + "</table></div>"
    return page(p["repo"], body)


async def _head(p):
    return await pipeline.fj.head(p["repo"], p["branch"])


@app.post("/projects/{pid}/redeploy", dependencies=[Depends(auth)])
async def redeploy(pid: int):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    await pipeline.handle_push(pid, await _head(p), force=True)
    return RedirectResponse(f"/projects/{pid}", 303)


@app.post("/projects/{pid}/allow", dependencies=[Depends(auth)])
async def allow(pid: int):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    db.run("UPDATE projects SET allowed_sha=? WHERE id=?", p["last_sha"], pid)
    await pipeline.handle_push(pid, p["last_sha"], force=True)
    return RedirectResponse(f"/projects/{pid}", 303)


@app.post("/projects/{pid}/stop", dependencies=[Depends(auth)])
async def stop(pid: int):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    await pipeline.cf.stop(p["app_uuid"])
    db.run("UPDATE projects SET status='stopped' WHERE id=?", pid)
    db.event(pid, "info", "Остановлен учителем.")
    return RedirectResponse(f"/projects/{pid}", 303)


@app.post("/projects/{pid}/start", dependencies=[Depends(auth)])
async def start(pid: int):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    await pipeline.handle_push(pid, await _head(p), force=True)
    return RedirectResponse(f"/projects/{pid}", 303)


@app.post("/projects/{pid}/secret", dependencies=[Depends(auth)])
async def secret(pid: int, key: str = Form(...), value: str = Form(...)):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    key, value = key.strip(), value.strip()
    if "TOKEN" in key.upper() and p["type"] in BOT_TYPES:
        ok, msg = await Telegram.check_token(value)
        db.event(pid, "info" if ok else "error", msg)
        if not ok:
            return RedirectResponse(f"/projects/{pid}", 303)
    await pipeline.cf.set_env(p["app_uuid"], key, value)
    db.event(pid, "info", f"Секрет {key} обновлён.")
    await pipeline.handle_push(pid, await _head(p), force=True)
    return RedirectResponse(f"/projects/{pid}", 303)


@app.post("/hooks/forgejo")
async def forgejo_hook(request: Request):
    body = await request.body()
    sig = request.headers.get("X-Forgejo-Signature") or request.headers.get("X-Gitea-Signature") or ""
    expected = hmac.new(CFG["WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()
    if not CFG["WEBHOOK_SECRET"] or not hmac.compare_digest(sig, expected):
        raise HTTPException(403, "bad signature")
    data = json.loads(body)
    repo = data.get("repository", {}).get("name")
    p = db.one("SELECT * FROM projects WHERE repo=?", repo)
    if not p or data.get("ref") != f"refs/heads/{p['branch']}" or not data.get("after"):
        return {"ignored": True}
    asyncio.create_task(pipeline.handle_push(p["id"], data["after"]))
    return {"queued": True}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
