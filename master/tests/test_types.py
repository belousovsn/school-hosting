import json
import unittest

from app.analyze import analyze, resolve
from app.checks import check_push, verdict


def run(files, runtime=None):
    return analyze(list(files), files, runtime)


class TypesTest(unittest.TestCase):
    def test_static_site_in_folder(self):
        files = {"site/index.html": "<h1>Привет</h1>", "site/style.css": "", "README.md": ""}
        a = run(files)
        self.assertEqual(a["type"], "static")
        c = resolve(a, {})["contract"]
        self.assertEqual((c["base_dir"], c["port"], c["start"]), ("site", 80, ""))

    def test_flask(self):
        files = {"web/app.py": "from flask import Flask\napp = Flask(__name__)\n", "web/requirements.txt": "flask\n",
                 "web/templates/index.html": "<p>x</p>"}
        a = run(files)
        self.assertEqual((a["type"], a["framework"]), ("python-web", "Flask"))
        self.assertEqual(a["runtimes"], ["python"])  # index.html в templates не считается отдельным сайтом
        c = resolve(a, {})["contract"]
        self.assertEqual(c["start"], "flask --app app:app run --host 0.0.0.0 --port 8000")
        self.assertEqual(c["base_dir"], "web")

    def test_fastapi_needs_uvicorn(self):
        files = {"main.py": "from fastapi import FastAPI\napi = FastAPI()\n", "requirements.txt": "fastapi\n"}
        a = run(files)
        self.assertIn("uvicorn", a["missing_packages"])
        self.assertEqual(resolve(a, {})["contract"]["start"], "uvicorn main:api --host 0.0.0.0 --port 8000")

    def test_node_express_with_start_script(self):
        pkg = {"scripts": {"start": "node server.js"}, "dependencies": {"express": "^4"}}
        files = {"package.json": json.dumps(pkg), "server.js": "const express = require('express');\nconst cors = require('cors');\napp.listen(5000)\n"}
        a = run(files)
        self.assertEqual((a["type"], a["framework"], a["port"]), ("node-web", "express", 5000))
        self.assertEqual(a["missing_packages"], ["cors"])
        c = resolve(a, {})["contract"]
        self.assertEqual((c["start"], c["port"]), ("", 5000))

    def test_node_bot_without_start(self):
        pkg = {"dependencies": {"grammy": "^1"}}
        files = {"bot/package.json": json.dumps(pkg), "bot/index.js": "import { Bot } from 'grammy'\nconst bot = new Bot(process.env.BOT_TOKEN)\nbot.start()\n"}
        a = run(files)
        self.assertEqual((a["type"], a["secrets"]), ("node-bot", ["BOT_TOKEN"]))
        c = resolve(a, {})["contract"]
        self.assertEqual((c["base_dir"], c["start"]), ("bot", "node index.js"))

    def test_python_and_node_asks(self):
        files = {"bot.py": "if __name__ == '__main__':\n    pass\n", "package.json": "{}", "index.js": ""}
        a = run(files)
        self.assertEqual(a["questions"][0]["id"], "runtime")
        self.assertEqual(run(files, "node")["runtime"], "node")

    def test_checks_static_and_node(self):
        def check(files):
            tree = [{"path": p, "type": "blob", "size": len(c)} for p, c in files.items()]
            return check_push(tree, lambda p: files[p])
        static = {"school.toml": b'type = "static"\nbase_dir = "site"\nentry = "index.html"\n', "site/index.html": b"<h1>x</h1>"}
        self.assertEqual(verdict(check(static)), "ok")
        node = {"school.toml": b'type = "node-web"\nbase_dir = ""\nentry = ""\n', "server.js": b""}
        self.assertEqual(check(node)[0]["text"], "Нет файла package.json.")


if __name__ == "__main__":
    unittest.main()


def test_parse_users():
    from app.main import parse_users
    assert parse_users("teacher", "p1", "test:test, x:") == {"teacher": "p1", "test": "test"}
    assert parse_users("teacher", "", "") == {}
