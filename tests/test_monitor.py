"""巡檢邏輯測試：在暫存資料夾造假的服務、log、DB。跑法：python -m unittest discover -s tests"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import monitor  # noqa: E402
import notify  # noqa: E402

NOW = time.time()


def touch(path: Path, hours_ago=0.0, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = NOW - hours_ago * 3600
    os.utime(path, (t, t))


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = self.root / "svc"
        touch(self.svc / "logs" / "app.log", hours_ago=1)
        con = sqlite3.connect(self.svc / "data.db")
        con.execute("CREATE TABLE t (x)")
        con.commit()
        con.close()
        touch(self.svc / "node_modules" / "ignored.log", hours_ago=999)
        self.cfg = dict(monitor.DEFAULTS, projects=[{"name": "示範服務", "path": str(self.svc)}],
                        disk_min_free_gb=0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_healthy_is_ok(self):
        r = monitor.run(self.cfg, now=NOW)
        self.assertTrue(r["ok"], r["alerts"])
        p = r["projects"][0]
        self.assertEqual(p["log_count"], 1)              # node_modules 被略過
        self.assertTrue(p["databases"][0]["openable"])

    def test_stale_log(self):
        touch(self.svc / "logs" / "app.log", hours_ago=30)
        r = monitor.run(self.cfg, now=NOW)
        self.assertFalse(r["ok"])
        self.assertIn("可能停了", r["alerts"][0])

    def test_per_project_threshold(self):
        self.cfg["projects"][0]["log_stale_hours"] = 0.5
        self.assertIn("可能停了", monitor.run(self.cfg, now=NOW)["alerts"][0])

    def test_missing_path_and_no_logs(self):
        empty = self.root / "empty"
        empty.mkdir()
        cfg = dict(self.cfg, projects=[{"name": "不見", "path": str(self.root / "nope")},
                                       {"name": "沒 log", "path": str(empty)},
                                       {"name": "不寫 log", "path": str(empty), "expect_logs": False}])
        alerts = monitor.run(cfg, now=NOW)["alerts"]
        self.assertEqual(len(alerts), 2)
        self.assertIn("路徑不見了", alerts[0])
        self.assertIn("沒有 log", alerts[1])

    def test_big_db_and_log(self):
        touch(self.svc / "logs" / "huge.log", hours_ago=0, size=2 * 1024 * 1024)
        cfg = dict(self.cfg, log_max_mb=1, db_max_mb=0.001)
        text = " ".join(monitor.run(cfg, now=NOW)["alerts"])
        self.assertIn("LOG 太大", text)
        self.assertIn("DB 太大", text)

    def test_corrupt_db(self):
        (self.svc / "broken.sqlite").write_bytes(b"not a database at all" * 100)
        text = " ".join(monitor.run(self.cfg, now=NOW)["alerts"])
        self.assertIn("DB 打不開", text)

    def test_db_probe_is_read_only(self):
        db = self.svc / "data.db"
        before = (db.stat().st_mtime, db.read_bytes())
        monitor.run(self.cfg, now=NOW)
        self.assertEqual(before, (db.stat().st_mtime, db.read_bytes()))
        self.assertFalse((self.svc / "data.db-journal").exists())

    def test_outputs_freshness(self):
        touch(self.root / "out" / "a.html", hours_ago=10)
        touch(self.root / "backup" / "old.zip", hours_ago=100)
        touch(self.root / "backup" / "sub" / "new.zip", hours_ago=2)
        cfg = dict(self.cfg, outputs=[
            {"name": "報表", "path": str(self.root / "out" / "a.html"), "max_age_hours": 5},
            {"name": "備份", "path": str(self.root / "backup"), "max_age_hours": 48},
            {"name": "不存在", "path": str(self.root / "zzz"), "max_age_hours": 48}])
        alerts = monitor.run(cfg, now=NOW)["alerts"]
        self.assertEqual(len(alerts), 2)
        self.assertIn("沒有產出", alerts[0])
        self.assertIn("產出不見了", alerts[1])

    def test_disk_threshold(self):
        cfg = dict(self.cfg, disk_min_free_gb=10 ** 9)
        self.assertIn("磁碟快滿", " ".join(monitor.run(cfg, now=NOW)["alerts"]))

    def test_delta_from_previous(self):
        first = monitor.run(self.cfg, now=NOW)
        first["projects"][0]["databases"][0]["size_mb"] -= 1
        second = monitor.run(self.cfg, now=NOW, prev=first)
        self.assertEqual(second["projects"][0]["databases"][0]["delta_mb"], 1)

    def test_main_writes_state_and_exit_code(self):
        cfg_file = self.root / "config.json"
        cfg_file.write_text(json.dumps(dict(self.cfg, data_dir=str(self.root / "data")), ensure_ascii=False),
                            encoding="utf-8")
        self.assertEqual(monitor.main(["--config", str(cfg_file), "--no-notify"]), 0)
        self.assertTrue((self.root / "data" / "state.json").exists())
        touch(self.svc / "logs" / "app.log", hours_ago=99)
        self.assertEqual(monitor.main(["--config", str(cfg_file), "--no-notify"]), 1)
        self.assertIn("alerts=1", (self.root / "data" / "last_run.txt").read_text(encoding="utf-8"))
        self.assertEqual(monitor.main(["--config", str(self.root / "missing.json")]), 2)

    def test_example_config_is_valid(self):
        cfg = monitor.load_config(ROOT / "config.example.json")
        self.assertEqual(len(cfg["projects"]), 3)
        self.assertIn("discord_webhook_env", cfg["notify"])


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in ("T_HOOK", "T_TOKEN", "T_TO")}

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_send_channels(self):
        calls = []
        post = lambda url, payload, headers=None: calls.append((url, payload, headers)) or True
        conf = {"discord_webhook_env": "T_HOOK", "line_token_env": "T_TOKEN", "line_to_env": "T_TO"}
        self.assertEqual(notify.send("hi", conf, post=post), [])
        os.environ.update(T_HOOK="https://example.invalid/hook", T_TOKEN="tok", T_TO="U" + "0" * 32)
        self.assertEqual(notify.send("x" * 3000, conf, post=post), ["discord", "line"])
        self.assertEqual(len(calls[0][1]["content"]), 1900)
        self.assertEqual(calls[1][2]["Authorization"], "Bearer tok")

    def test_failure_does_not_raise(self):
        os.environ["T_HOOK"] = "https://example.invalid/hook"

        def boom(*a, **k):
            raise OSError("down")
        self.assertEqual(notify.send("hi", {"discord_webhook_env": "T_HOOK"}, post=boom), [])


if __name__ == "__main__":
    unittest.main()
