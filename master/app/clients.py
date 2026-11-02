"""Тонкие клиенты к Forgejo, Coolify, Telegram и Docker."""

from __future__ import annotations

import base64
import os
from urllib.parse import quote

import httpx

CFG = {k: os.environ.get(k, "") for k in (
    "CF_API_URL", "CF_TOKEN", "CF_PROJECT_UUID", "CF_SERVER_UUID", "CF_KEY_UUID",
    "FORGEJO_URL", "FORGEJO_TOKEN", "FORGEJO_SSH", "ORG", "MASTER_URL", "WEBHOOK_SECRET",
    "NOTIFY_TG_TOKEN", "NOTIFY_TG_CHAT", "APPS_DOMAIN",
)}
CFG["ORG"] = CFG["ORG"] or "school"
# Лимиты по типам проектов: (память, доля ядра).
LIMITS = {"static": ("64m", "0.25"), "python-web": ("256m", "0.5"), "node-web": ("256m", "0.5")}
DEFAULT_LIMITS = ("256m", "0.25")


def site_url(repo: str) -> str:
    """Адрес сайта ученика. APPS_DOMAIN вида 'https://school.ru' или '1.2.3.4.sslip.io'."""
    d = CFG["APPS_DOMAIN"].strip().rstrip("/")
    scheme, host = (d.split("://", 1) if "://" in d else ("http", d))
    return f"{scheme}://{repo.lower().replace('_', '-').replace('.', '-')}.{host}"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ApiError(RuntimeError):
    pass


def _check(r: httpx.Response) -> httpx.Response:
    if r.status_code >= 400:
        raise ApiError(f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}")
    return r


class Forgejo:
    def __init__(self):
        self.c = httpx.AsyncClient(
            base_url=CFG["FORGEJO_URL"].rstrip("/") + "/api/v1",
            headers={"Authorization": f"token {CFG['FORGEJO_TOKEN']}"},
            timeout=TIMEOUT,
        )
        self.org = CFG["ORG"]

    async def repo(self, name: str) -> dict | None:
        r = await self.c.get(f"/repos/{self.org}/{name}")
        return None if r.status_code == 404 else _check(r).json()

    async def migrate(self, url: str, name: str) -> dict:
        body = {"clone_addr": url, "repo_name": name, "repo_owner": self.org, "private": True, "mirror": False}
        if "github.com" in url:
            body["service"] = "github"
        return _check(await self.c.post("/repos/migrate", json=body, timeout=120)).json()

    async def tree(self, name: str, ref: str) -> list[dict]:
        out, page = [], 1
        while True:
            r = _check(await self.c.get(f"/repos/{self.org}/{name}/git/trees/{ref}", params={"recursive": "true", "per_page": 1000, "page": page}))
            data = r.json()
            out += data.get("tree", [])
            if not data.get("truncated"):
                return out
            page += 1

    async def raw(self, name: str, path: str, ref: str) -> bytes:
        r = await self.c.get(f"/repos/{self.org}/{name}/raw/{quote(path)}", params={"ref": ref})
        return _check(r).content

    async def head(self, name: str, branch: str = "main") -> str:
        r = _check(await self.c.get(f"/repos/{self.org}/{name}/branches/{branch}"))
        return r.json()["commit"]["id"]

    async def has_commit(self, name: str, sha: str) -> bool:
        r = await self.c.get(f"/repos/{self.org}/{name}/git/commits/{sha}", params={"stat": "false", "files": "false"})
        return r.status_code == 200

    def push_url(self, name: str) -> str:
        """Адрес для git push с токеном внутри. Токен в выводе git прячется."""
        scheme, rest = CFG["FORGEJO_URL"].rstrip("/").split("://", 1)
        return f"{scheme}://{CFG['FORGEJO_TOKEN']}@{rest}/{self.org}/{name}.git"

    async def default_branch(self, name: str) -> str:
        return (await self.repo(name))["default_branch"]

    async def commit_files(self, name: str, branch: str, files: dict[str, str], message: str) -> str:
        ops = []
        for path, content in files.items():
            r = await self.c.get(f"/repos/{self.org}/{name}/contents/{quote(path)}", params={"ref": branch})
            op = {"path": path, "content": base64.b64encode(content.encode()).decode()}
            if r.status_code == 200:
                op |= {"operation": "update", "sha": r.json()["sha"]}
            else:
                op["operation"] = "create"
            ops.append(op)
        body = {"branch": branch, "message": message, "files": ops,
                "author": {"name": "Мастер подключения", "email": "master@school.local"}}
        return _check(await self.c.post(f"/repos/{self.org}/{name}/contents", json=body)).json()["commit"]["sha"]

    async def remove_hooks(self, name: str):
        hooks = _check(await self.c.get(f"/repos/{self.org}/{name}/hooks")).json()
        for h in hooks:
            await self.c.delete(f"/repos/{self.org}/{name}/hooks/{h['id']}")

    async def set_master_hook(self, name: str):
        await self.remove_hooks(name)  # прямые вебхуки в Coolify убираем: сборку запускает только мастер
        body = {"type": "gitea", "active": True, "events": ["push"],
                "config": {"url": CFG["MASTER_URL"].rstrip("/") + "/hooks/forgejo", "content_type": "json", "secret": CFG["WEBHOOK_SECRET"]}}
        _check(await self.c.post(f"/repos/{self.org}/{name}/hooks", json=body))


class Coolify:
    def __init__(self):
        self.c = httpx.AsyncClient(
            base_url=CFG["CF_API_URL"].rstrip("/") + "/api/v1",
            headers={"Authorization": f"Bearer {CFG['CF_TOKEN']}"},
            timeout=TIMEOUT,
        )

    async def create_app(self, repo: str, branch: str, contract: dict, name: str | None = None) -> tuple[str, str]:
        """Создаёт приложение под тип проекта. name: имя проекта, по умолчанию как у репозитория.
        Возвращает (uuid, адрес сайта или '')."""
        name = name or repo
        kind = contract["type"]
        web = kind in ("static", "python-web", "node-web")
        body = {
            "project_uuid": CFG["CF_PROJECT_UUID"], "server_uuid": CFG["CF_SERVER_UUID"],
            "environment_name": "production", "private_key_uuid": CFG["CF_KEY_UUID"],
            "git_repository": f"{CFG['FORGEJO_SSH'].rstrip('/')}/{CFG['ORG']}/{repo}.git",
            "git_branch": branch, "build_pack": "static" if kind == "static" else "nixpacks",
            "ports_exposes": str(contract.get("port") or (80 if kind == "static" else 3000)),
            "base_directory": "/" + (contract.get("base_dir") or "").strip("/"),
            "name": name, "instant_deploy": False,
        }
        if contract.get("start"):
            body["start_command"] = contract["start"]
        uuid = _check(await self.c.post("/applications/private-deploy-key", json=body)).json()["uuid"]
        url = site_url(name) if web else ""
        mem, cpu = LIMITS.get(kind, DEFAULT_LIMITS)
        await self.update_app(uuid, {"domains": url, "limits_memory": mem, "limits_cpus": cpu, "health_check_enabled": False})
        if kind.startswith("python") or kind == "telegram-bot":
            await self.set_env(uuid, "PYTHONUNBUFFERED", "1")
        if kind == "node-web":
            await self.set_env(uuid, "PORT", str(contract.get("port") or 3000))
        return uuid, url

    async def update_app(self, uuid: str, data: dict):
        _check(await self.c.patch(f"/applications/{uuid}", json=data))

    async def set_env(self, uuid: str, key: str, value: str):
        r = await self.c.post(f"/applications/{uuid}/envs", json={"key": key, "value": value})
        if r.status_code >= 400:
            _check(await self.c.patch(f"/applications/{uuid}/envs", json={"key": key, "value": value}))

    async def env_keys(self, uuid: str) -> set[str]:
        r = _check(await self.c.get(f"/applications/{uuid}/envs"))
        return {e["key"] for e in r.json()}

    async def deploy(self, uuid: str, force: bool = False) -> str:
        r = _check(await self.c.post("/deploy", params={"uuid": uuid, "force": str(force).lower()}))
        return r.json()["deployments"][0]["deployment_uuid"]

    async def deployment(self, dep_uuid: str) -> dict:
        return _check(await self.c.get(f"/deployments/{dep_uuid}")).json()

    async def latest_deployment(self, app_uuid: str) -> dict | None:
        """Coolify склеивает одинаковые запросы на сборку, поэтому смотрим последнюю сборку приложения."""
        r = _check(await self.c.get(f"/deployments/applications/{app_uuid}", params={"take": 1}))
        items = r.json().get("deployments", [])
        return items[0] if items else None

    async def app(self, uuid: str) -> dict:
        return _check(await self.c.get(f"/applications/{uuid}")).json()

    async def stop(self, uuid: str):
        _check(await self.c.post(f"/applications/{uuid}/stop"))

    async def start(self, uuid: str):
        _check(await self.c.post(f"/applications/{uuid}/start"))

    async def create_postgres(self, name: str) -> tuple[str, str]:
        """Создаёт PostgreSQL с ночной резервной копией. Возвращает (uuid, внутренний адрес для DATABASE_URL)."""
        body = {"server_uuid": CFG["CF_SERVER_UUID"], "project_uuid": CFG["CF_PROJECT_UUID"],
                "environment_name": "production", "name": name, "postgres_db": "app",
                "limits_memory": "256m", "limits_cpus": "0.5", "instant_deploy": True}
        d = _check(await self.c.post("/databases/postgresql", json=body)).json()
        await self.c.post(f"/databases/{d['uuid']}/backups", json={
            "frequency": "0 3 * * *", "enabled": True, "database_backup_retention_amount_locally": 7})
        return d["uuid"], d["internal_db_url"]

    async def delete_database(self, uuid: str):
        r = await self.c.delete(f"/databases/{uuid}", params={
            "delete_configurations": "true", "delete_volumes": "true",
            "docker_cleanup": "true", "delete_connected_networks": "true"})
        if r.status_code != 404:
            _check(r)

    async def database_status(self, uuid: str) -> str:
        r = await self.c.get(f"/databases/{uuid}")
        return "missing" if r.status_code == 404 else _check(r).json().get("status", "")

    async def delete(self, uuid: str):
        """Удаляет приложение вместе с контейнером и томами."""
        r = await self.c.delete(f"/applications/{uuid}", params={
            "delete_configurations": "true", "delete_volumes": "true",
            "docker_cleanup": "true", "delete_connected_networks": "true"})
        if r.status_code != 404:
            _check(r)


class Telegram:
    @staticmethod
    async def call(token: str, method: str, **params) -> dict:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"https://api.telegram.org/bot{token}/{method}", params=params)
            return r.json()

    @classmethod
    async def check_token(cls, token: str) -> tuple[bool, str]:
        """Проверяет токен и снимает старый webhook, который мешает polling."""
        me = await cls.call(token, "getMe")
        if not me.get("ok"):
            return False, "Telegram не принял токен. Проверь, что скопирован целиком."
        info = await cls.call(token, "getWebhookInfo")
        note = ""
        if info.get("result", {}).get("url"):
            await cls.call(token, "deleteWebhook")
            note = " У бота был webhook, я его снял."
        return True, f"Бот @{me['result']['username']} найден.{note}"


async def notify(text: str):
    if CFG["NOTIFY_TG_TOKEN"] and CFG["NOTIFY_TG_CHAT"]:
        try:
            await Telegram.call(CFG["NOTIFY_TG_TOKEN"], "sendMessage", chat_id=CFG["NOTIFY_TG_CHAT"], text=text)
        except httpx.HTTPError:
            pass


class Docker:
    """Только чтение статистики через docker.sock."""

    def __init__(self, sock: str = "/var/run/docker.sock"):
        self.enabled = os.path.exists(sock)
        self.c = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=sock), base_url="http://docker", timeout=TIMEOUT) if self.enabled else None

    async def container_for(self, app_uuid: str) -> dict | None:
        r = await self.c.get("/containers/json", params={"filters": f'{{"name":["{app_uuid}"]}}'})
        items = _check(r).json()
        return items[0] if items else None

    async def state(self, cid: str) -> dict:
        info = _check(await self.c.get(f"/containers/{cid}/json")).json()
        return {"status": info["State"]["Status"], "restarts": info.get("RestartCount", 0)}

    async def logs(self, cid: str, tail: int = 15) -> str:
        raw = _check(await self.c.get(f"/containers/{cid}/logs", params={"stdout": 1, "stderr": 1, "tail": tail})).content
        out, i = [], 0
        while i + 8 <= len(raw):  # поток Docker: 8 байт заголовка на каждый кусок
            size = int.from_bytes(raw[i + 4:i + 8], "big")
            out.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        return b"".join(out).decode("utf-8", errors="replace") if out else raw.decode("utf-8", errors="replace")

    async def cpu_usage(self, cid: str) -> tuple[float, float]:
        """Возвращает (загрузка в долях ядра, лимит в долях ядра)."""
        s = _check(await self.c.get(f"/containers/{cid}/stats", params={"stream": "false"})).json()
        cpu, pre = s["cpu_stats"], s["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre.get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        cores = cpu.get("online_cpus") or 1
        used = (cpu_delta / sys_delta) * cores if sys_delta > 0 else 0.0
        info = _check(await self.c.get(f"/containers/{cid}/json")).json()
        limit = (info["HostConfig"].get("NanoCpus") or 0) / 1e9 or float(cores)
        return used, limit
