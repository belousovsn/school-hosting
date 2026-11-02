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
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import db, pipeline
from .analyze import BOT_TYPES, TYPES, WEB_TYPES
from .clients import CFG, ApiError, Telegram, site_url


def parse_users(main_user: str, main_password: str, extra: str) -> dict[str, str]:
    """Основной учитель плюс дополнительные из строки вида 'логин:пароль,логин2:пароль2'."""
    users = {main_user: main_password} if main_password else {}
    for pair in extra.split(","):
        login, _, password = pair.strip().partition(":")
        if login and password:
            users[login] = password
    return users


USERS = parse_users(os.environ.get("MASTER_USER", "teacher"), os.environ.get("MASTER_PASSWORD", ""),
                    os.environ.get("MASTER_EXTRA_USERS", ""))
security = HTTPBasic()


def auth(cred: HTTPBasicCredentials = Depends(security)):
    expected = USERS.get(cred.username)
    if not (expected and secrets.compare_digest(cred.password.encode(), expected.encode())):
        raise HTTPException(401, "Нужен вход", headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(pipeline.cpu_monitor()), asyncio.create_task(pipeline.github_watcher())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

STATUS = {
    "new": ("Новый", "muted"), "deploying": ("Собирается", "info"), "running": ("Работает", "ok"),
    "failed": ("Сборка упала", "bad"), "blocked": ("Заблокирован", "bad"),
    "suspicious": ("Ждёт решения учителя", "warn"), "stopped": ("Остановлен", "muted"),
    "crashing": ("Падает после запуска", "bad"), "web_down": ("Сайт не открывается", "bad"),
}
# Как часто страница переспрашивает статус, мс. Пока идёт сборка, чаще.
POLL = {"deploying": 2000, "new": 2000, "web_down": 8000, "crashing": 10000}
POLL_IDLE = 15000

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2433;--muted:#6b7280;--line:#e5e7eb;--ok:#15803d;--bad:#b91c1c;--warn:#b45309;--info:#1d4ed8;--accent:#2563eb;--okbg:#ecfdf3}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1a1d24;--ink:#e6e8ec;--muted:#9aa1ad;--line:#2b2f38;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--info:#93c5fd;--accent:#60a5fa;--okbg:#14261b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 16px}h2{font-size:17px;margin:24px 0 8px}
a{color:var(--accent)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:12px}
.card.flash{background:var(--okbg);border-color:var(--ok)}
table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.info{color:var(--info)}.muted{color:var(--muted)}
input[type=text],input[type=password],input[type=url]{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer;display:inline-flex;align-items:center;gap:8px}
button:disabled{opacity:.7;cursor:progress}button.ghost{background:transparent;color:var(--accent);border:1px solid var(--accent)}
button.danger{background:transparent;color:var(--bad);border:1px solid var(--bad)}form.inline{display:inline}
ul{margin:4px 0;padding-left:20px}code{font-size:13px}nav{margin-bottom:16px}label{display:block;margin:4px 0}
.spin{display:inline-block;width:12px;height:12px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite;vertical-align:-1px;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}@media (prefers-reduced-motion:reduce){.spin{animation-duration:2.5s}}
.progress{margin-top:10px;padding:10px 12px;border-radius:8px;background:var(--bg);border:1px solid var(--line)}
.progress code{display:block;margin-top:4px;color:var(--muted);white-space:pre-wrap;word-break:break-word}
.hint{margin:8px 0 0}.live-note{font-size:13px;margin-top:8px}
"""

# Кнопка сразу показывает, что идёт работа. Блоки с data-live сами подтягивают свежее состояние.
JS = """
let leaving = false;
document.addEventListener('submit', e => {
  const f = e.target;
  if (f.dataset.confirm && !confirm(f.dataset.confirm)) { e.preventDefault(); return; }
  leaving = true;
  const b = e.submitter || f.querySelector('button');
  if (b) b.innerHTML = '<span class="spin"></span>' + (f.dataset.busy || 'Подождите…');
  const hint = f.querySelector('.hint[hidden]'); if (hint) hint.hidden = false;
  setTimeout(() => document.querySelectorAll('button').forEach(x => x.disabled = true), 0);
});
window.addEventListener('pageshow', e => { if (e.persisted) location.reload(); });
const t0 = Date.now();
function tickTimers() {
  document.querySelectorAll('[data-elapsed]').forEach(el => {
    const s = Math.max(0, Math.floor(+el.dataset.elapsed + (Date.now() - (+el.dataset.t || t0)) / 1000));
    el.textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  });
}
setInterval(tickTimers, 1000);
document.querySelectorAll('[data-live]').forEach(el => {
  let delay = +el.dataset.every || 5000;
  const note = document.getElementById('live-note');
  const tick = async () => {
    if (leaving) return;
    if (!document.hidden) {
      try {
        const r = await fetch(el.dataset.live, {cache: 'no-store'});
        if (r.status === 404) { location.href = '/'; return; }
        if (r.ok && !leaving) {
          el.innerHTML = await r.text();
          el.querySelectorAll('[data-elapsed]').forEach(x => x.dataset.t = Date.now());
          delay = +(r.headers.get('X-Poll') || delay);
          if (note) note.textContent = 'Состояние обновляется само.';
        }
      } catch (_) { if (note) note.textContent = 'Нет связи с сервером, пробую ещё раз.'; }
    }
    setTimeout(tick, delay);
  };
  setTimeout(tick, delay);
});
"""


def page(title: str, body: str, msg: str = "") -> HTMLResponse:
    flash = f'<div class="card flash">{escape(msg)}</div>' if msg else ""
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title><style>{CSS}</style></head>
<body><main><nav><a href="/">Проекты</a> · <a href="/connect">Подключить проект</a></nav><h1>{escape(title)}</h1>{flash}{body}</main>
<script>{JS}</script></body></html>""")


def fragment(html: str, statuses: list[str]) -> HTMLResponse:
    poll = min([POLL.get(s, POLL_IDLE) for s in statuses] or [POLL_IDLE])
    return HTMLResponse(html, headers={"X-Poll": str(poll), "Cache-Control": "no-store"})


def go(path: str, msg: str = "") -> RedirectResponse:
    return RedirectResponse(path + (f"?msg={quote(msg)}" if msg else ""), 303)


def elapsed(started: float) -> str:
    secs = int(time.time() - started) if started else 0
    return f'<span data-elapsed="{secs}">{secs // 60}:{secs % 60:02d}</span>'


def status_badge(p: dict, short: bool = False) -> str:
    label, cls = STATUS.get(p["status"], (p["status"], "muted"))
    pr = pipeline.progress(p["id"]) if p["status"] == "deploying" else None
    if p["status"] == "deploying":
        extra = f' · {escape(pr["stage"])} · {elapsed(pr["started"])}' if pr and short else ""
        return f'<span class="{cls}"><span class="spin"></span>{label}{extra}</span>'
    return f'<span class="{cls}">{label}</span>'


def items(lst, cls="") -> str:
    return "<ul>" + "".join(f'<li class="{cls}">{escape(x)}</li>' for x in lst) + "</ul>" if lst else ""


async def refreshed(rows: list[dict]) -> list[dict]:
    return list(await asyncio.gather(*(pipeline.refresh_status(p) for p in rows)))


def project_rows(projects: list[dict]) -> str:
    out = ""
    for p in projects:
        link = f'<a href="{escape(p["url"])}" target="_blank">открыть</a>' if p.get("url") else ""
        kind = TYPES.get(p["type"], p["type"] or "")
        out += f'<tr><td><a href="/projects/{p["id"]}">{escape(p["repo"])}</a></td><td>{escape(kind)}</td><td>{status_badge(p, short=True)}</td><td>{link}</td></tr>'
    return out


@app.get("/", dependencies=[Depends(auth)])
async def index(msg: str = ""):
    projects = await refreshed(db.all_("SELECT * FROM projects ORDER BY id DESC"))
    if not projects:
        return page("Проекты", '<div class="card">Пока ни одного проекта. <a href="/connect">Подключить первый</a>.</div>', msg)
    every = min(POLL.get(p["status"], POLL_IDLE) for p in projects)
    body = f'''<div class="card"><table><thead><tr><th>Проект</th><th>Тип</th><th>Состояние</th><th>Сайт</th></tr></thead>
<tbody data-live="/live/projects" data-every="{every}">{project_rows(projects)}</tbody></table>
<p class="muted live-note" id="live-note">Состояние обновляется само.</p></div>'''
    return page("Проекты", body, msg)


@app.get("/live/projects", dependencies=[Depends(auth)])
async def live_projects():
    projects = await refreshed(db.all_("SELECT * FROM projects ORDER BY id DESC"))
    return fragment(project_rows(projects), [p["status"] for p in projects])


@app.get("/connect", dependencies=[Depends(auth)])
async def connect_form(error: str = ""):
    err = f'<p class="bad">{escape(error)}</p>' if error else ""
    return page("Подключить проект", f"""<div class="card">{err}
<form method="post" action="/connect" data-busy="Копирую и разбираю проект…"><label>Ссылка на репозиторий (GitHub или наш Forgejo)</label>
<input type="url" name="url" required placeholder="https://github.com/user/my-project">
<p class="muted">Подходят Telegram-боты и сайты на Python (Flask, FastAPI, Django), сайты и боты на Node.js, статические сайты на HTML.</p>
<p class="muted">Мастер скопирует репозиторий в Forgejo, разберёт код и спросит только то, что не смог понять сам.</p>
<button>Разобрать проект</button>
<p class="hint muted" hidden>Копирую репозиторий в Forgejo и читаю код. Обычно это занимает 10–30 секунд, большой репозиторий может копироваться дольше.</p>
</form></div>""")


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
               f'<form method="post" action="/drafts/{d["id"]}/recheck" data-busy="Проверяю код заново…"><button class="ghost">Я исправил, проверить снова</button></form></div>'
        return out
    out += f'<form method="post" action="/drafts/{d["id"]}" data-busy="Подключаю проект…"><div class="card">'
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
    out += '''<button>Подключить и запустить</button>
<p class="hint muted" hidden>Проверяю токен, сохраняю настройки в репозиторий и создаю приложение на сервере. Это 10–20 секунд, потом откроется страница проекта, где будет видно, как идёт сборка.</p></form>'''
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
        return go(f"/projects/{pid}", "Проект подключён, сборка запущена. Ход сборки виден ниже.")
    d = db.one("SELECT * FROM drafts WHERE id=?", did)
    return page("Подключение проекта", draft_body(d, errors))


def project_live(p: dict) -> str:
    """Часть страницы проекта, которая обновляется сама: состояние, проверки, журнал."""
    pid = p["id"]
    findings = json.loads(p["findings"] or "[]")
    body = f'<div class="card">Состояние: <b>{status_badge(p)}</b><br>Репозиторий: <a href="{escape(CFG["FORGEJO_URL"])}/{CFG["ORG"]}/{escape(p["repo"])}">{CFG["ORG"]}/{escape(p["repo"])}</a>'
    body += f'<br>Тип: {escape(TYPES.get(p["type"], p["type"] or ""))}'
    if p.get("url"):
        body += f'<br>Сайт: <a href="{escape(p["url"])}" target="_blank">{escape(p["url"])}</a>'
    body += f'<br>Последний коммит: <code>{escape((p["last_sha"] or "")[:8])}</code>'
    if p.get("source"):
        got = f', последний забранный коммит <code>{escape(p["source_sha"][:8])}</code>' if p.get("source_sha") else ""
        body += f'<br>Код берётся из <a href="{escape(p["source"])}" target="_blank">{escape(p["source"])}</a>, ' \
                f'новые коммиты проверяю раз в {pipeline.SYNC_MINUTES} мин{got}.'
    if p["status"] == "deploying":
        pr = pipeline.progress(pid)
        if pr:
            body += f'<div class="progress"><b>{escape(pr["stage"])}</b> <span class="muted">· идёт {elapsed(pr["started"])}</span>'
            body += f'<code>{escape(pr["line"])}</code></div>' if pr["line"] else "</div>"
        else:
            body += '<div class="progress">Отправляю на сборку…</div>'
    body += "</div>"
    if findings:
        body += '<div class="card"><b>Проверка последнего коммита</b><ul>' + "".join(
            f'<li class="{"bad" if f["level"] == "block" else "warn"}">{escape(f["text"])}</li>' for f in findings) + "</ul>"
        if p["status"] == "suspicious":
            body += f'<form method="post" action="/projects/{pid}/allow" data-busy="Запускаю сборку…"><button class="ghost">Я посмотрел код, разрешить этот коммит</button></form>'
        body += "</div>"
    ev = db.all_("SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT 30", pid)
    body += "<h2>Журнал</h2><div class='card'><table>" + "".join(
        f'<tr><td class="muted">{time.strftime("%d.%m %H:%M", time.localtime(e["ts"]))}</td><td class="{ {"error": "bad", "warn": "warn"}.get(e["level"], "")}">{escape(e["text"])}</td></tr>'
        for e in ev) + "</table></div>"
    return body


async def _project(pid: int) -> dict:
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    if not p:
        raise HTTPException(404)
    return await pipeline.refresh_status(p)


@app.get("/projects/{pid}", dependencies=[Depends(auth)])
async def project(pid: int, msg: str = ""):
    p = await _project(pid)
    try:
        await pipeline.project_source(p)  # у старых проектов узнаём, что они из GitHub
    except ApiError:
        pass
    p = db.one("SELECT * FROM projects WHERE id=?", pid) | {"status": p["status"]}
    every = POLL.get(p["status"], POLL_IDLE)
    body = f'<div data-live="/live/projects/{pid}" data-every="{every}">{project_live(p)}</div>'
    github = ""
    if p.get("source"):
        github = f"""<form class="inline" method="post" action="/projects/{pid}/github" data-busy="Забираю код из GitHub…"><button>Обновить из GitHub</button></form>
"""
    note = '<p class="muted hint">Изменения, отправленные прямо в Forgejo, перезапишутся при следующем обновлении из GitHub.</p>' if p.get("source") else ""
    body += f"""<p class="muted live-note" id="live-note">Состояние обновляется само.</p>
<div class="card">{github}
<form class="inline" method="post" action="/projects/{pid}/redeploy" data-busy="Проверяю код…"><button{' class="ghost"' if github else ''}>Проверить и пересобрать</button></form>
<form class="inline" method="post" action="/projects/{pid}/stop" data-busy="Останавливаю…"><button class="ghost">Остановить</button></form>
<form class="inline" method="post" action="/projects/{pid}/start" data-busy="Запускаю…"><button class="ghost">Запустить</button></form>{note}</div>
<div class="card"><b>Обновить секрет</b><form method="post" action="/projects/{pid}/secret" data-busy="Сохраняю и перезапускаю…">
<label>Имя<input type="text" name="key" value="BOT_TOKEN"></label><label>Значение<input type="password" name="value" autocomplete="off"></label>
<button class="ghost">Сохранить и перезапустить</button></form></div>
<div class="card"><b>Удалить проект</b><p class="muted">Приложение и его данные на сервере удалятся. Код останется в Forgejo, проект можно будет подключить заново.</p>
<form method="post" action="/projects/{pid}/delete" data-busy="Удаляю…" data-confirm="Удалить проект {escape(p['repo'])} с сервера?">
<button class="danger">Удалить с сервера</button></form></div>"""
    return page(p["repo"], body, msg)


@app.get("/live/projects/{pid}", dependencies=[Depends(auth)])
async def live_project(pid: int):
    p = await _project(pid)
    return fragment(project_live(p), [p["status"]])


async def _head(p):
    return await pipeline.fj.head(p["repo"], p["branch"])


def _after_push(pid: int) -> str:
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    return {"deploying": "Код проверен, сборка запущена.", "suspicious": "Сборка остановлена: в коде есть подозрительное, подробности ниже.",
            "blocked": "Сборка запрещена: подробности ниже."}.get(p["status"], "")


@app.post("/projects/{pid}/redeploy", dependencies=[Depends(auth)])
async def redeploy(pid: int):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    await pipeline.handle_push(pid, await _head(p), force=True)
    return go(f"/projects/{pid}", _after_push(pid))


@app.post("/projects/{pid}/allow", dependencies=[Depends(auth)])
async def allow(pid: int):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    db.run("UPDATE projects SET allowed_sha=? WHERE id=?", p["last_sha"], pid)
    await pipeline.handle_push(pid, p["last_sha"], force=True)
    return go(f"/projects/{pid}", _after_push(pid))


@app.post("/projects/{pid}/stop", dependencies=[Depends(auth)])
async def stop(pid: int):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    await pipeline.cf.stop(p["app_uuid"])
    db.run("UPDATE projects SET status='stopped' WHERE id=?", pid)
    db.event(pid, "info", "Остановлен учителем.")
    return go(f"/projects/{pid}", "Проект остановлен.")


@app.post("/projects/{pid}/start", dependencies=[Depends(auth)])
async def start(pid: int):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    await pipeline.handle_push(pid, await _head(p), force=True)
    return go(f"/projects/{pid}", _after_push(pid))


@app.post("/projects/{pid}/secret", dependencies=[Depends(auth)])
async def secret(pid: int, key: str = Form(...), value: str = Form(...)):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    key, value = key.strip(), value.strip()
    if "TOKEN" in key.upper() and p["type"] in BOT_TYPES:
        ok, msg = await Telegram.check_token(value)
        db.event(pid, "info" if ok else "error", msg)
        if not ok:
            return go(f"/projects/{pid}", "Токен не сохранён: " + msg)
    await pipeline.cf.set_env(p["app_uuid"], key, value)
    db.event(pid, "info", f"Секрет {key} обновлён.")
    await pipeline.handle_push(pid, await _head(p), force=True)
    return go(f"/projects/{pid}", f"Секрет {key} сохранён. " + _after_push(pid))


@app.post("/projects/{pid}/github", dependencies=[Depends(auth)])
async def github(pid: int):
    try:
        msg = await pipeline.check_github(pid)
    except Exception as e:
        msg = f"Не получилось забрать код из GitHub: {e}"
        db.event(pid, "warn", msg)
        return go(f"/projects/{pid}", msg)
    extra = _after_push(pid) if msg.startswith("Забрал") else ""
    return go(f"/projects/{pid}", f"{msg} {extra}".strip())


@app.post("/projects/{pid}/delete", dependencies=[Depends(auth)])
async def delete(pid: int):
    p = db.one("SELECT * FROM projects WHERE id=?", pid)
    if not p:
        return go("/")
    await pipeline.delete_project(pid)
    return go("/", f"Проект {p['repo']} удалён с сервера. Код остался в Forgejo.")


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
