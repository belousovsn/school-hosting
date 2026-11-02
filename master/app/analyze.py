"""Разбор чужого проекта: тип, папка, команда запуска, порт, зависимости, секреты.

Только стандартная библиотека, чтобы модуль можно было тестировать без сервиса.
Сборкой занимается Coolify (Nixpacks или Static). Мастер находит то, что Nixpacks
сам не угадает: проект в подпапке, имя переменной токена, адрес и порт сайта.
"""

from __future__ import annotations

import json
import posixpath
import re
import sys
import tomllib

STDLIB = set(sys.stdlib_module_names)
IGNORE_DIRS = {"venv", ".venv", "env", "__pycache__", "node_modules", ".git", "site-packages", "build", "dist", ".next"}
WEB_PORT = 8000  # порт, на котором мастер запускает Python-сайты

TYPES = {
    "telegram-bot": "Telegram-бот на Python",
    "python-web": "Сайт на Python",
    "python-app": "Python-программа без сайта",
    "node-web": "Сайт на Node.js",
    "node-bot": "Telegram-бот на Node.js",
    "static": "Статический сайт",
}
BOT_TYPES = {"telegram-bot", "node-bot"}
WEB_TYPES = {"python-web", "node-web", "static"}

IMPORT_TO_PKG = {
    "telegram": "python-telegram-bot", "telebot": "pyTelegramBotAPI", "dotenv": "python-dotenv",
    "yaml": "PyYAML", "PIL": "Pillow", "bs4": "beautifulsoup4", "sklearn": "scikit-learn",
    "cv2": "opencv-python", "dateutil": "python-dateutil", "jwt": "PyJWT",
}
PY_BOT_LIBS = {"aiogram": "aiogram", "telegram": "python-telegram-bot", "telebot": "pyTelegramBotAPI"}
PY_WEB_LIBS = {"fastapi": "FastAPI", "flask": "Flask", "django": "Django"}
NODE_BOT_LIBS = {"telegraf", "grammy", "node-telegram-bot-api"}
NODE_WEB_LIBS = {"express", "fastify", "koa", "@hapi/hapi", "next", "hono"}
NODE_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "crypto", "dgram", "dns", "events", "fs", "http", "http2",
    "https", "net", "os", "path", "perf_hooks", "process", "querystring", "readline", "stream", "string_decoder",
    "timers", "tls", "url", "util", "v8", "vm", "worker_threads", "zlib", "fs/promises", "module", "async_hooks",
}

TG_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
PY_ENV_RE = re.compile(
    r"""os\.(?:getenv|environ\.get)\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""
    r"""|os\.environ\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""
)
JS_ENV_RE = re.compile(r"""process\.env\.([A-Za-z_][A-Za-z0-9_]*)|process\.env\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]""")
POLL_RE = re.compile(r"start_polling|run_polling|infinity_polling|\.polling\(|run_webhook")
MAIN_RE = re.compile(r"""if\s+__name__\s*==\s*['"]__main__['"]""")
PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_]\w*)[\w.]*\s+import|import\s+([A-Za-z_]\w*))", re.M)
PY_APP_RE = re.compile(r"^(\w+)\s*=\s*(Flask|FastAPI)\(", re.M)
JS_IMPORT_RE = re.compile(r"""require\(\s*['"]([^'"./][^'"]*)['"]\s*\)|from\s+['"]([^'"./][^'"]*)['"]|import\s+['"]([^'"./][^'"]*)['"]""")
JS_LISTEN_RE = re.compile(r"\.listen\(\s*(\d{2,5})")
SQLITE_RE = re.compile(r"""sqlite3\.connect\(\s*['"]([^'"]+)['"]""")
WRITE_RE = re.compile(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*['"][wa]""")
SECRET_WORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASS")
PY_ENTRY_NAMES = ("main.py", "bot.py", "app.py", "run.py", "server.py", "__main__.py")
JS_ENTRY_NAMES = ("index.js", "server.js", "app.js", "bot.js", "main.js")


def ignored(path: str) -> bool:
    return any(part in IGNORE_DIRS for part in path.split("/"))


def norm_pkg(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(text: str) -> set[str]:
    pkgs = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[\s@]", line, maxsplit=1)[0]
        if name:
            pkgs.add(norm_pkg(name))
    return pkgs


def find_hardcoded_tokens(files: dict[str, str]) -> list[tuple[str, int]]:
    return [(p, i) for p, t in files.items() for i, line in enumerate(t.splitlines(), 1) if TG_TOKEN_RE.search(line)]


def _secrets(env_vars: list[str]) -> list[str]:
    return [v for v in env_vars if any(w in v.upper() for w in SECRET_WORDS) and v != "PORT"]


def _shallowest(paths: list[str]) -> list[str]:
    return sorted(paths, key=lambda p: (p.count("/"), p))


def needed_for_analysis(path: str) -> bool:
    """Файлы, которые нужно прочитать, чтобы разобрать проект."""
    return path.endswith((".py", ".env", ".json", ".cfg", ".ini", ".js", ".mjs", ".cjs", ".ts")) \
        or posixpath.basename(path) in ("requirements.txt", "school.toml")


def detect_runtime(paths: list[str]) -> list[str]:
    """Какие среды есть в репозитории, по порядку предпочтения."""
    found = []
    if any(p.endswith(".py") for p in paths):
        found.append("python")
    if any(posixpath.basename(p) == "package.json" for p in paths):
        found.append("node")
    # index.html внутри templates/, public/ и т.п. обычно часть Python- или Node-сайта.
    asset_dirs = {"templates", "static", "public", "assets", "src", "dist", "build"}
    pages = [p for p in paths if posixpath.basename(p) == "index.html"]
    if any(not (set(p.split("/")[:-1]) & asset_dirs) for p in pages) or (pages and not found):
        found.append("static")
    return found


def analyze(paths: list[str], files: dict[str, str], runtime: str | None = None) -> dict:
    """paths: все файлы; files: содержимое исходников и конфигов. runtime выбирает учитель, если сред несколько."""
    paths = [p for p in paths if not ignored(p)]
    files = {p: t for p, t in files.items() if not ignored(p)}
    runtimes = detect_runtime(paths)
    base = {"problems": [], "warnings": [], "questions": [], "runtimes": runtimes}
    if "school.toml" in files:
        try:
            base["contract"] = tomllib.loads(files["school.toml"])
        except tomllib.TOMLDecodeError as e:
            base["problems"].append(f"school.toml не читается: {e}")
    if not runtimes:
        base |= {"type": "unknown", "entry_candidates": [], "secrets": []}
        base["problems"].append("Не понял, что это за проект: нет ни .py, ни package.json, ни index.html.")
        return base
    rt = runtime if runtime in runtimes else runtimes[0]
    base["runtime"] = rt
    if len(runtimes) > 1:
        base["questions"].append({"id": "runtime", "text": "В репозитории несколько видов кода. Что запускать?",
                                  "options": [rt] + [r for r in runtimes if r != rt], "labels": {"python": "Python", "node": "Node.js", "static": "Статический сайт (HTML)"}})
    if rt == "python":
        return base | _analyze_python(paths, files, base)
    if rt == "node":
        return base | _analyze_node(paths, files, base)
    st = _analyze_static(paths)
    st["questions"] = base["questions"] + st["questions"]
    return base | st


def _analyze_static(paths: list[str]) -> dict:
    idx = _shallowest([p for p in paths if posixpath.basename(p) == "index.html"])
    q = []
    if len(idx) > 1:
        q.append({"id": "entry", "text": "Какая страница главная?", "options": idx})
    return {"type": "static", "framework": None, "entry_candidates": idx, "req_candidates": [],
            "secrets": [], "env_vars": [], "missing_packages": [], "data_paths": [], "hardcoded_tokens": [],
            "questions": q}


def _analyze_python(paths: list[str], files: dict[str, str], base: dict) -> dict:
    py = {p: t for p, t in files.items() if p.endswith(".py")}
    problems, warnings, questions = list(base["problems"]), list(base["warnings"]), list(base["questions"])
    imports = {a or b for t in py.values() for a, b in PY_IMPORT_RE.findall(t)}

    framework, kind = None, "python-app"
    for mod, fw in PY_BOT_LIBS.items():
        if mod in imports:
            framework, kind = fw, "telegram-bot"
            break
    else:
        for mod, fw in PY_WEB_LIBS.items():
            if mod in imports:
                framework, kind = fw, "python-web"
                break

    # Файл запуска: у сайта файл, где создаётся app, у остальных файл с polling или __main__.
    if kind == "python-web" and framework == "Django":
        candidates = [p for p in paths if posixpath.basename(p) == "manage.py"]
        warnings.append("Для Django в settings.py нужно разрешить адрес сайта: ALLOWED_HOSTS = [\"*\"].")
    elif kind == "python-web":
        candidates = _shallowest([p for p, t in py.items() if PY_APP_RE.search(t)])
    else:
        def score(p, t):
            return 3 * bool(POLL_RE.search(t)) + bool(MAIN_RE.search(t)) + (posixpath.basename(p) in PY_ENTRY_NAMES)
        scored = sorted(((p, score(p, t)) for p, t in py.items()), key=lambda x: (-x[1], x[0].count("/"), x[0]))
        candidates = [p for p, s in scored if s >= 2] or [p for p, s in scored if s >= 1]
    if len(candidates) > 1:
        questions.append({"id": "entry", "text": "Какой файл запускать?", "options": candidates})
    elif not candidates:
        problems.append("Не нашёл файл запуска." + (" Нет строки вида app = Flask(...) или app = FastAPI(...)." if kind == "python-web"
                        else " Нет ни start_polling/run_polling, ни блока if __name__ == '__main__'."))

    reqs = _shallowest([p for p in paths if posixpath.basename(p) == "requirements.txt"])
    if len(reqs) > 1:
        questions.append({"id": "requirements", "text": "Какой requirements.txt использовать?", "options": reqs})
    declared = set().union(*(parse_requirements(files.get(r, "")) for r in reqs)) if reqs else set()
    local = {posixpath.splitext(posixpath.basename(p))[0] for p in py} | {part for p in py for part in p.split("/")[:-1]}
    needed = [IMPORT_TO_PKG.get(m, m) for m in sorted(imports) if m not in STDLIB and m not in local and m != "__future__"]
    if framework == "FastAPI":
        needed.append("uvicorn")  # мастер запускает FastAPI через uvicorn
    missing = [p for p in dict.fromkeys(needed) if norm_pkg(p) not in declared]
    if not reqs:
        warnings.append("Нет requirements.txt. Создам его из импортов, проверь список.")
    elif missing:
        warnings.append("В requirements.txt не хватает пакетов: " + ", ".join(missing))

    env_vars = sorted({a or b for t in py.values() for a, b in PY_ENV_RE.findall(t)})
    secrets = _secrets(env_vars)
    tokens = find_hardcoded_tokens({p: t for p, t in files.items() if p.endswith((".py", ".env", ".json", ".toml", ".cfg", ".ini"))})
    _token_problems(kind, tokens, secrets, problems, "os.getenv(\"BOT_TOKEN\")")

    data_paths = sorted({m for t in py.values() for m in SQLITE_RE.findall(t) + WRITE_RE.findall(t)})
    if data_paths:
        warnings.append("Программа пишет в файлы (" + ", ".join(data_paths[:5]) + "). Сейчас они сбрасываются при каждом обновлении.")
    return {"type": kind, "framework": framework, "entry_candidates": candidates, "req_candidates": reqs,
            "missing_packages": missing, "env_vars": env_vars, "secrets": secrets, "hardcoded_tokens": tokens,
            "data_paths": data_paths, "problems": problems, "warnings": warnings, "questions": questions,
            "app_vars": {p: PY_APP_RE.search(t).group(1) for p, t in py.items() if PY_APP_RE.search(t)}}


def _analyze_node(paths: list[str], files: dict[str, str], base: dict) -> dict:
    problems, warnings, questions = list(base["problems"]), list(base["warnings"]), list(base["questions"])
    pkgs = _shallowest([p for p in paths if posixpath.basename(p) == "package.json"])
    if len(pkgs) > 1:
        questions.append({"id": "requirements", "text": "Какой package.json использовать?", "options": pkgs})
    pkg_path = pkgs[0]
    try:
        pkg = json.loads(files.get(pkg_path, "{}") or "{}")
    except json.JSONDecodeError as e:
        pkg = {}
        problems.append(f"{pkg_path} не читается: {e}")
    deps = set(pkg.get("dependencies", {})) | set(pkg.get("devDependencies", {}))
    js = {p: t for p, t in files.items() if p.endswith((".js", ".mjs", ".cjs", ".ts"))}

    kind, framework = "node-web", None
    if deps & NODE_BOT_LIBS:
        kind, framework = "node-bot", sorted(deps & NODE_BOT_LIBS)[0]
    elif deps & NODE_WEB_LIBS:
        framework = sorted(deps & NODE_WEB_LIBS)[0]
    elif not any(JS_LISTEN_RE.search(t) or ".listen(" in t for t in js.values()):
        kind = "node-bot" if any("telegram" in t.lower() for t in js.values()) else "node-web"

    # Запуск: npm start, если скрипт есть; иначе main или типовое имя файла.
    pkg_dir = posixpath.dirname(pkg_path)
    has_start = bool(pkg.get("scripts", {}).get("start"))
    candidates = []
    if not has_start:
        main = pkg.get("main")
        if main and posixpath.normpath(posixpath.join(pkg_dir, main)) in paths:
            candidates = [posixpath.normpath(posixpath.join(pkg_dir, main))]
        else:
            candidates = [posixpath.join(pkg_dir, n) if pkg_dir else n for n in JS_ENTRY_NAMES
                          if (posixpath.join(pkg_dir, n) if pkg_dir else n) in paths]
        if len(candidates) > 1:
            questions.append({"id": "entry", "text": "Какой файл запускать?", "options": candidates})
        elif not candidates:
            problems.append("В package.json нет скрипта start и не нашёл index.js/server.js/app.js. Добавь \"start\": \"node <файл>\" в scripts.")

    used = set()
    for t in js.values():
        for a, b, c in JS_IMPORT_RE.findall(t):
            name = (a or b or c)
            if name.startswith("node:"):
                continue
            name = "/".join(name.split("/")[:2]) if name.startswith("@") else name.split("/")[0]
            if name not in NODE_BUILTINS:
                used.add(name)
    missing = sorted(used - deps)
    if missing:
        warnings.append("В package.json нет пакетов, которые подключаются в коде: " + ", ".join(missing))

    env_vars = sorted({a or b for t in js.values() for a, b in JS_ENV_RE.findall(t)})
    secrets = _secrets(env_vars)
    tokens = find_hardcoded_tokens({p: t for p, t in files.items() if p.endswith((".js", ".mjs", ".cjs", ".ts", ".json", ".env"))})
    _token_problems(kind, tokens, secrets, problems, "process.env.BOT_TOKEN")

    port = 3000
    if kind == "node-web":
        ports = {int(m) for t in js.values() for m in JS_LISTEN_RE.findall(t)}
        uses_env_port = "PORT" in env_vars
        if ports and not uses_env_port:
            port = sorted(ports)[0]
        if not ports and not uses_env_port and not framework == "next":
            warnings.append("Не нашёл, на каком порту слушает сайт. Поставлю 3000 и передам его в переменной PORT.")
    return {"type": kind, "framework": framework, "entry_candidates": candidates, "req_candidates": pkgs,
            "missing_packages": missing, "env_vars": env_vars, "secrets": secrets, "hardcoded_tokens": tokens,
            "data_paths": [], "problems": problems, "warnings": warnings, "questions": questions,
            "has_start_script": has_start, "port": port}


def _token_problems(kind: str, tokens, secrets, problems: list[str], how: str):
    if tokens:
        where = ", ".join(f"{p}:{n}" for p, n in tokens[:5])
        problems.append(f"Токен бота записан прямо в коде ({where}). Репозиторий мог быть публичным, поэтому токен считается засвеченным: "
                        f"сделай /revoke в @BotFather и замени строку на {how}.")
    elif kind in BOT_TYPES and not secrets:
        problems.append(f"Не нашёл, откуда бот берёт токен. Код должен читать его так: {how}.")


def _module(entry_rel: str) -> str:
    return posixpath.splitext(entry_rel)[0].replace("/", ".")


def resolve(analysis: dict, answers: dict) -> dict:
    """Собирает контракт из анализа и ответов учителя."""
    kind = analysis["type"]
    entry = answers.get("entry") or (analysis["entry_candidates"][0] if analysis["entry_candidates"] else None)
    req = answers.get("requirements") or (analysis["req_candidates"][0] if analysis.get("req_candidates") else None)
    contract = {"type": kind, "secrets": analysis.get("secrets", []), "start": "", "port": 0}

    if kind == "static":
        if not entry:
            return {"contract": None, "errors": ["Не нашёл index.html."]}
        contract |= {"base_dir": posixpath.dirname(entry), "entry": "index.html", "port": 80}
        return {"contract": contract, "errors": [], "create_requirements": False}

    if kind.startswith("node"):
        base = posixpath.dirname(req) if req else ""
        rel = posixpath.relpath(entry, base or ".") if entry else ""
        errors = [f"Файл запуска {entry} лежит вне папки с package.json."] if rel.startswith("..") else []
        if not analysis.get("has_start_script") and not entry:
            errors.append("Не выбран файл запуска.")
        contract |= {"base_dir": base, "entry": rel, "start": "" if analysis.get("has_start_script") else f"node {rel}",
                     "port": analysis.get("port", 3000) if kind == "node-web" else 0}
        return {"contract": contract, "errors": errors, "create_requirements": False}

    if not entry:
        return {"contract": None, "errors": ["Не выбран файл запуска."]}
    base = posixpath.dirname(req) if req else posixpath.dirname(entry)
    rel = posixpath.relpath(entry, base or ".")
    errors = [f"Файл запуска {entry} лежит вне папки с requirements.txt ({base or 'корень'})."] if rel.startswith("..") else []
    contract |= {"base_dir": base, "entry": rel, "python": "3.12"}
    if kind == "python-web":
        fw = analysis.get("framework")
        var = analysis.get("app_vars", {}).get(entry, "app")
        if fw == "FastAPI":
            contract["start"] = f"uvicorn {_module(rel)}:{var} --host 0.0.0.0 --port {WEB_PORT}"
        elif fw == "Flask":
            contract["start"] = f"flask --app {_module(rel)}:{var} run --host 0.0.0.0 --port {WEB_PORT}"
        else:  # Django
            contract["start"] = f"python {rel} runserver 0.0.0.0:{WEB_PORT}"
        contract["port"] = WEB_PORT
    else:
        contract["start"] = f"python {rel}"
    return {"contract": contract, "errors": errors, "create_requirements": req is None}


def contract_toml(c: dict) -> str:
    secrets = ", ".join(f'"{s}"' for s in c.get("secrets", []))
    lines = [
        "# Описание проекта для школьного сервера. Меняет учитель или мастер подключения.",
        f'type = "{c["type"]}"   # {TYPES.get(c["type"], "")}',
        f'base_dir = "{c.get("base_dir", "")}"   # папка проекта внутри репозитория',
        f'entry = "{c.get("entry", "")}"',
        f'start = "{c.get("start", "")}"   # пусто: команду выберет сборщик',
        f'port = {int(c.get("port") or 0)}',
    ]
    if c.get("python"):
        lines.append(f'python = "{c["python"]}"')
    lines.append(f"secrets = [{secrets}]   # значения хранятся только на сервере")
    return "\n".join(lines) + "\n"
