"""通知節流、重試、分段、憑證遮蔽、設定檔驗證、唯讀與規模測試。不連外網（webhook 用本機隨機 port 的假伺服器）。"""
import http.server
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import monitor  # noqa: E402
import notify  # noqa: E402

NOW = time.time()


def touch(path: Path, hours_ago=0.0, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - hours_ago * 3600
    os.utime(path, (t, t))


class FakeWebhook:
    """本機假 webhook：依序回應 statuses，記錄收到的內容。"""

    def __init__(self, statuses=(204,)):
        self.statuses, self.received, self.hits = list(statuses), [], 0
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                code = outer.statuses[min(outer.hits, len(outer.statuses) - 1)]
                outer.hits += 1
                if code < 300:
                    outer.received.append(json.loads(body))
                self.send_response(code)
                if code == 429:
                    self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/hook"
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class PlanTest(unittest.TestCase):
    def rep(self, alerts):
        return {"alerts": [t for _, t in alerts], "alert_keys": [k for k, _ in alerts], "generated_at": "T"}

    def test_new_alert_notifies_then_is_suppressed_then_reminds_then_recovers(self):
        r = self.rep([("stale:a", "[可能停了] a")])
        text, st = monitor.plan_notification(r, {}, 1000, 72)
        self.assertIn("a", text)
        text2, st2 = monitor.plan_notification(r, st, 1000 + 3600 * 24, 72)     # 隔一天：同樣異常不重複通知
        self.assertIsNone(text2)
        text3, st3 = monitor.plan_notification(r, st, 1000 + 3600 * 73, 72)     # 超過 72 小時：再提醒一次
        self.assertIn("a", text3)
        text4, st4 = monitor.plan_notification(self.rep([]), st3, 1000 + 3600 * 80, 72)   # 好了：已恢復
        self.assertIn("已恢復", text4)
        self.assertEqual(st4, {})
        self.assertIsNone(monitor.plan_notification(self.rep([]), st4, 1000 + 3600 * 90, 72)[0])

    def test_changed_numbers_are_same_alert(self):
        """訊息裡的數字（幾小時沒更新）每天都在變，但仍是同一個異常。"""
        _, st = monitor.plan_notification(self.rep([("stale:a", "已 27.0 小時")]), {}, 1000, 72)
        text, _ = monitor.plan_notification(self.rep([("stale:a", "已 51.0 小時")]), st, 1000 + 86400, 72)
        self.assertIsNone(text)

    def test_only_new_alerts_are_listed_with_quiet_count(self):
        _, st = monitor.plan_notification(self.rep([("k1", "A")]), {}, 1000, 72)
        text, st2 = monitor.plan_notification(self.rep([("k1", "A"), ("k2", "B")]), st, 2000, 72)
        self.assertIn("B", text)
        self.assertNotIn("• A", text)
        self.assertIn("另有 1 項", text)
        self.assertEqual(set(st2), {"k1", "k2"})

    def test_renotify_zero_means_every_run(self):
        r = self.rep([("k", "A")])
        _, st = monitor.plan_notification(r, {}, 1000, 0)
        self.assertIsNotNone(monitor.plan_notification(r, st, 1001, 0)[0])

    def test_run_emits_stable_keys(self):
        with tempfile.TemporaryDirectory() as d:
            touch(Path(d) / "svc" / "a.log", hours_ago=99)
            cfg = dict(monitor.DEFAULTS, projects=[{"name": "S", "path": str(Path(d) / "svc")}], disk_min_free_gb=0)
            r1 = monitor.run(cfg, now=time.time())
            r2 = monitor.run(cfg, now=time.time() + 86400)
            self.assertEqual(r1["alert_keys"], r2["alert_keys"])
            self.assertNotEqual(r1["alerts"], r2["alerts"])
            self.assertEqual(len(r1["alert_keys"]), len(r1["alerts"]))


class EndToEndNotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        touch(self.root / "svc" / "logs" / "app.log", hours_ago=99)
        self.hook = FakeWebhook([204])
        os.environ["T_HOOK2"] = self.hook.url
        cfg = dict(monitor.DEFAULTS, projects=[{"name": "示範服務", "path": str(self.root / "svc")}],
                   disk_min_free_gb=0, data_dir=str(self.root / "data"), notify={"discord_webhook_env": "T_HOOK2"})
        self.cfg_file = self.root / "config.json"
        self.cfg_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.saved_defaults = notify._post.__defaults__
        notify._post.__defaults__ = (None, 10, 3, lambda s: None)   # 重試不真的等

    def tearDown(self):
        notify._post.__defaults__ = self.saved_defaults
        os.environ.pop("T_HOOK2", None)
        self.hook.close()
        self.tmp.cleanup()

    def run_main(self):
        return monitor.main(["--config", str(self.cfg_file)])

    def test_same_anomaly_notifies_once_across_daily_runs_then_recovery(self):
        for _ in range(5):                         # 模擬連續 5 天都有同一個異常
            self.assertEqual(self.run_main(), 1)   # 結束代碼仍是 1（有異常），只是不重複推播
        self.assertEqual(len(self.hook.received), 1)
        self.assertIn("可能停了", self.hook.received[0]["content"])
        touch(self.root / "svc" / "logs" / "app.log", hours_ago=0)   # 修好了
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.hook.received), 2)
        self.assertIn("已恢復", self.hook.received[1]["content"])
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.hook.received), 2)                 # 恢復只通知一次

    def test_failed_delivery_is_retried_next_run_not_marked_notified(self):
        self.hook.statuses = [404]                 # 4xx：不重試，也不能記成「已通知」
        self.run_main()
        self.assertEqual(self.hook.hits, 1)
        self.assertFalse((self.root / "data" / "notify_state.json").exists())
        self.hook.statuses, self.hook.hits = [204], 0
        self.run_main()
        self.assertEqual(len(self.hook.received), 1)                 # 下次巡檢補送

    def test_retry_on_429_and_5xx_then_success(self):
        self.hook.statuses = [429, 503, 204]
        self.run_main()
        self.assertEqual(self.hook.hits, 3)
        self.assertEqual(len(self.hook.received), 1)

    def test_gives_up_after_three_attempts(self):
        self.hook.statuses = [500]
        self.run_main()
        self.assertEqual(self.hook.hits, 3)
        self.assertIn("通知失敗 discord", (self.root / "data" / "last_run.txt").read_text(encoding="utf-8"))

    def test_noisy_second_day_new_alert_is_sent(self):
        self.run_main()
        touch(self.root / "svc" / "big.log", hours_ago=99, size=3 * 1024 * 1024)
        cfg = json.loads(self.cfg_file.read_text(encoding="utf-8"))
        cfg["log_max_mb"] = 1
        self.cfg_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.run_main()
        self.assertEqual(len(self.hook.received), 2)
        self.assertIn("LOG 太大", self.hook.received[1]["content"])
        self.assertNotIn("• [可能停了]", self.hook.received[1]["content"])


class NotifyUnitTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in ("T_H", "T_T", "T_TO")}

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_secrets_redacted_from_errors(self):
        hook = "https://discord.com/api/webhooks/1234567890/SECRETTOKEN_abc-DEF"
        os.environ.update(T_H=hook, T_T="LINETOKEN1234567890", T_TO="U" + "9" * 32)
        conf = {"discord_webhook_env": "T_H", "line_token_env": "T_T", "line_to_env": "T_TO"}

        def boom(url, payload, headers=None):
            raise urllib.error.URLError(f"cannot reach {url} with Bearer {headers and headers.get('Authorization')}"
                                        f" to {payload.get('to')}")
        errs = []
        import io
        old = sys.stderr
        sys.stderr = io.StringIO()
        try:
            notify.send("x", conf, post=boom, errors=errs)
            err_out = sys.stderr.getvalue()
        finally:
            sys.stderr = old
        blob = "\n".join(errs) + err_out
        for secret in ("SECRETTOKEN_abc-DEF", "LINETOKEN1234567890", "9" * 32):
            self.assertNotIn(secret, blob)
        self.assertEqual(len(errs), 2)

    def test_non_https_webhook_rejected(self):
        for bad in ("file:///C:/Windows/win.ini", "ftp://x/y", "http://evil.example/hook", "javascript:alert(1)"):
            os.environ["T_H"] = bad
            errs = []
            called = []
            self.assertEqual(notify.send("x", {"discord_webhook_env": "T_H"},
                                         post=lambda *a, **k: called.append(1), errors=errs), [])
            self.assertEqual(called, [])
            self.assertEqual(len(errs), 1)

    def test_chunking_preserves_all_content_up_to_limit(self):
        lines = [f"• 異常 {i} " + "x" * 80 for i in range(40)]      # 約 3.6k 字
        text = "\n".join(lines)
        chunks = notify.chunk_text(text, 1900)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= 1900 for c in chunks))
        self.assertEqual("\n".join(chunks), text)

    def test_chunking_overflow_note_and_limits(self):
        text = "\n".join(f"line {i} " + "y" * 90 for i in range(400))   # 遠超過 3 則
        chunks = notify.chunk_text(text, 1900)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 1900 for c in chunks))
        self.assertIn("沒顯示", chunks[-1])

    def test_discord_disables_mentions(self):
        sent = []
        notify.discord("@everyone hi", "https://example.invalid/h", post=lambda u, p, h=None: sent.append(p) or True)
        self.assertEqual(sent[0]["allowed_mentions"], {"parse": []})

    def test_post_does_not_retry_4xx(self):
        w = FakeWebhook([401])
        try:
            with self.assertRaises(urllib.error.HTTPError):
                notify._post(w.url, {"a": 1}, sleep=lambda s: None)
            self.assertEqual(w.hits, 1)
        finally:
            w.close()


class ConfigAndScaleTest(unittest.TestCase):
    def test_bad_configs_give_friendly_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            for i, content in enumerate(['{"projects": [ {"name": "x"} ]}', "{not json", "[1,2]",
                                         '{"projects": "nope"}', '{"db_max_mb": "big"}']):
                f = Path(d) / f"c{i}.json"
                f.write_text(content, encoding="utf-8")
                self.assertEqual(monitor.main(["--config", str(f), "--no-notify"]), 2, content)

    def test_config_path_with_dotdot_is_just_a_path_not_an_escape(self):
        """設定檔是信任的本機檔案；.. 只會被當成要監看的路徑（唯讀 stat），不會讀寫其內容。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a").mkdir()
            touch(root / "secret.txt", 0)
            cfg = dict(monitor.DEFAULTS, projects=[{"name": "x", "path": str(root / "a" / ".." / "a")}],
                       disk_min_free_gb=0)
            r = monitor.run(cfg)
            self.assertTrue(r["projects"][0]["exists"])
            self.assertEqual((root / "secret.txt").read_bytes(), b"x" * 10)

    def test_probe_readonly_with_live_wal_writer_and_odd_path(self):
        with tempfile.TemporaryDirectory(prefix="hm 測試 #1") as d:
            db = Path(d) / "資料 庫.db"
            w = sqlite3.connect(db, isolation_level=None)
            try:
                w.execute("PRAGMA journal_mode=WAL")
                w.execute("create table t(x)")
                w.execute("begin immediate")
                w.executemany("insert into t values(?)", [("x" * 200,)] * 500)
                before = {p.name: p.stat().st_mtime_ns for p in Path(d).iterdir()}
                ok, size, err = monitor.probe_db(db)
                after = {p.name: p.stat().st_mtime_ns for p in Path(d).iterdir()}
                self.assertTrue(ok, err)
                self.assertEqual(before, after)      # 沒新增檔案、沒改任何檔案的時間
                w.execute("commit")
            finally:
                w.close()

    def test_scale_100_projects_x_200_files(self):
        with tempfile.TemporaryDirectory() as d:
            projs = []
            for i in range(100):
                p = Path(d) / f"s{i}"
                (p / "logs").mkdir(parents=True)
                for j in range(200):
                    (p / "logs" / f"f{j}.txt").write_bytes(b"x")
                (p / "logs" / "app.log").write_bytes(b"x")
                c = sqlite3.connect(p / "d.db")
                c.execute("create table t(x)")
                c.commit()
                c.close()
                projs.append({"name": f"s{i}", "path": str(p)})
            cfg = dict(monitor.DEFAULTS, projects=projs, disk_min_free_gb=0)
            t = time.time()
            r = monitor.run(cfg)
            self.assertTrue(r["ok"], r["alerts"][:3])
            self.assertLess(time.time() - t, 15)


if __name__ == "__main__":
    unittest.main()
