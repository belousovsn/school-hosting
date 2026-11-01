#!/usr/bin/env bash
# Выкладывает изменённые файлы мастера в Forgejo (infra/deploy-master) и пересобирает его в Coolify.
# Запуск из папки master: ../scripts/push-master.sh app/main.py app/pipeline.py ...
set -euo pipefail
CRED="${CRED:-$(cd "$(dirname "$0")/.." && pwd)/school-test-credentials.md}"
FJT=$(sed -n 's/^- API-токен school-admin (для Claude): //p' "$CRED")
T=$(sed -n 's/^- Токен: //p' "$CRED")
APP=aglezlw7kazlkkg9avmqr461
python - "$FJT" "$@" > "${TEMP:-/tmp}/push.json" <<'PY'
import base64, json, pathlib, sys, urllib.request, urllib.error
tok, paths = sys.argv[1], sys.argv[2:]
ops = []
for p in paths:
    req = urllib.request.Request(f"http://git.159.89.104.179.sslip.io/api/v1/repos/infra/deploy-master/contents/{p}?ref=main", headers={"Authorization": f"token {tok}"})
    try:
        sha = json.load(urllib.request.urlopen(req))["sha"]
        op = {"operation": "update", "sha": sha}
    except urllib.error.HTTPError:
        op = {"operation": "create"}
    ops.append(op | {"path": p, "content": base64.b64encode(pathlib.Path(p).read_bytes()).decode()})
print(json.dumps({"branch": "main", "message": "Обновление мастера: " + ", ".join(paths), "files": ops}))
PY
curl -s -H "Authorization: token $FJT" -H "Content-Type: application/json" -X POST http://git.159.89.104.179.sslip.io/api/v1/repos/infra/deploy-master/contents --data-binary @"${TEMP:-/tmp}/push.json" | python -c "import sys,json;print('commit', json.load(sys.stdin)['commit']['sha'][:8])"
rm -f "${TEMP:-/tmp}/push.json"
DEP=$(curl -s -H "Authorization: Bearer $T" -X POST "http://159.89.104.179:8000/api/v1/deploy?uuid=$APP" | python -c "import sys,json;print(json.load(sys.stdin)['deployments'][0]['deployment_uuid'])")
ssh -o BatchMode=yes school-test "timeout 500 bash -c 'while true; do s=\$(curl -s -H \"Authorization: Bearer $T\" http://localhost:8000/api/v1/deployments/$DEP | python3 -c \"import sys,json;print(json.load(sys.stdin).get(\\\"status\\\"))\"); case \$s in finished|failed|cancelled*) echo master deploy: \$s; break;; esac; sleep 8; done'"
