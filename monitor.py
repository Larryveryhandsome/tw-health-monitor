"""每日健康巡檢：純唯讀，沒異常就安靜，有異常才通知。

檢查：
  1. 專案最新 log 多久沒更新（太久＝服務可能停了）
  2. SQLite 資料庫大小、能不能打開（唯讀 immutable 模式，不會鎖到正在寫的服務）
  3. 單一 log 檔太大（沒有輪替、無限長大）
  4. 磁碟剩餘空間
  5. 「應該定期更新」的產出檔多久沒更新（排程顯示成功、其實沒產出）

用法：
  python monitor.py              巡檢一次，健康就不輸出（exit 0），有異常印出並通知（exit 1）
  python monitor.py --report     不管健康與否都印完整 JSON
  python monitor.py --no-notify  有異常也只印、不推通知
  python monitor.py --config 路徑  指定設定檔（預設 config.json）
"""
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MB, GB = 1024 ** 2, 1024 ** 3

DEFAULTS = {
    "log_stale_hours": 26,
    "db_max_mb": 500,
    "log_max_mb": 50,
    "disk_min_free_gb": 10,
    "skip_dirs": [".git", "__pycache__", ".venv", "venv", "env", "node_modules", ".idea", ".mypy_cache"],
    "db_suffixes": [".db", ".sqlite", ".sqlite3"],
    "log_suffixes": [".log"],
    "projects": [],
    "outputs": [],
    "extra_disks": [],
    "notify": {},
}


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def _iso(epoch):
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def scan_project(root, cfg):
    """遞迴列出 (db 清單, log 清單)，每筆 (路徑, 大小, 修改時間)。只 stat，不打開。"""
    dbs, logs = [], []
    skip = set(cfg["skip_dirs"])
    db_suf, log_suf = tuple(cfg["db_suffixes"]), tuple(cfg["log_suffixes"])
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            low = fn.lower()
            if low.endswith(db_suf):
                dbs.append((full, st.st_size, st.st_mtime))
            elif low.endswith(log_suf):
                logs.append((full, st.st_size, st.st_mtime))
    return dbs, logs


def probe_db(path):
    """immutable 唯讀打開 SQLite：零寫入、零上鎖。回傳 (能否打開, 邏輯大小, 錯誤)。"""
    try:
        con = sqlite3.connect(Path(path).as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1.0)
        try:
            pages = con.execute("PRAGMA page_count").fetchone()[0]
            size = con.execute("PRAGMA page_size").fetchone()[0]
            con.execute("PRAGMA schema_version").fetchone()
            return True, int(pages) * int(size), None
        finally:
            con.close()
    except Exception as e:
        return False, None, str(e)


def newest_mtime(path):
    """檔案 → 修改時間；資料夾 → 裡面最新一個檔的修改時間；不存在 → None。"""
    p = Path(path)
    if p.is_file():
        return p.stat().st_mtime
    if p.is_dir():
        newest = None
        for root, _d, files in os.walk(p):
            for fn in files:
                try:
                    m = os.path.getmtime(os.path.join(root, fn))
                except OSError:
                    continue
                newest = m if newest is None or m > newest else newest
        return newest
    return None


def disks_for(cfg):
    roots = set()
    for item in cfg["projects"] + cfg["outputs"]:
        anchor = Path(item["path"]).anchor
        if anchor:
            roots.add(anchor)
    roots.update(cfg.get("extra_disks", []))
    return sorted(roots)


def run(cfg: dict, now: float = None, prev: dict = None) -> dict:
    now = now or time.time()
    prev_db = {}
    for pj in (prev or {}).get("projects", []):
        for d in pj.get("databases", []):
            prev_db[d["path"]] = d.get("size_mb")

    alerts, projects = [], []
    for pj in cfg["projects"]:
        name, root = pj["name"], pj["path"]
        stale_h = pj.get("log_stale_hours", cfg["log_stale_hours"])
        rep = {"name": name, "path": root, "exists": os.path.isdir(root), "newest_log": None,
               "log_count": 0, "databases": [], "large_logs": []}
        projects.append(rep)
        if not rep["exists"]:
            alerts.append(f"[路徑不見了] {name}：{root}")
            continue
        dbs, logs = scan_project(root, cfg)
        rep["log_count"] = len(logs)
        if logs:
            path, _s, mtime = max(logs, key=lambda t: t[2])
            age = (now - mtime) / 3600
            rep["newest_log"] = {"path": path, "mtime": _iso(mtime), "age_hours": round(age, 2), "stale": age > stale_h}
            if age > stale_h:
                alerts.append(f"[可能停了] {name}：最新 log 已 {age:.1f} 小時沒更新（門檻 {stale_h}h）→ {path}")
        elif pj.get("expect_logs", True):
            alerts.append(f"[沒有 log] {name}：找不到任何 log，無法判斷是否在運作")
        for path, size, _m in sorted(dbs, key=lambda t: -t[1]):
            ok, logical, err = probe_db(path)
            entry = {"path": path, "size_mb": round(size / MB, 2), "openable": ok,
                     "logical_mb": round(logical / MB, 2) if logical is not None else None}
            if isinstance(prev_db.get(path), (int, float)):
                entry["delta_mb"] = round(entry["size_mb"] - prev_db[path], 2)
            if err:
                entry["error"] = err
                alerts.append(f"[DB 打不開] {name}：{os.path.basename(path)}（{err}）")
            if size / MB > cfg["db_max_mb"]:
                alerts.append(f"[DB 太大] {name}：{os.path.basename(path)} = {size / MB:.1f}MB（門檻 {cfg['db_max_mb']}MB）")
            rep["databases"].append(entry)
        for path, size, _m in logs:
            if size / MB > cfg["log_max_mb"]:
                rep["large_logs"].append({"path": path, "size_mb": round(size / MB, 2)})
                alerts.append(f"[LOG 太大] {name}：{os.path.basename(path)} = {size / MB:.1f}MB（門檻 {cfg['log_max_mb']}MB）")

    outputs = []
    for it in cfg["outputs"]:
        maxh = float(it.get("max_age_hours", 48))
        m = newest_mtime(it["path"])
        entry = {"name": it["name"], "path": it["path"], "max_age_hours": maxh,
                 "mtime": _iso(m) if m else None, "age_hours": round((now - m) / 3600, 2) if m else None}
        outputs.append(entry)
        if m is None:
            alerts.append(f"[產出不見了] {it['name']}：找不到 {it['path']}")
        elif (now - m) / 3600 > maxh:
            alerts.append(f"[沒有產出] {it['name']}：最後更新 {datetime.fromtimestamp(m):%Y-%m-%d %H:%M}"
                          f"（已 {(now - m) / 3600:.0f} 小時，門檻 {maxh:.0f}h），排程可能悄悄壞了")

    disks = []
    for root in disks_for(cfg):
        try:
            u = shutil.disk_usage(root)
        except OSError as e:
            disks.append({"disk": root, "error": str(e)})
            continue
        free = u.free / GB
        disks.append({"disk": root, "free_gb": round(free, 2), "total_gb": round(u.total / GB, 2),
                      "used_pct": round(u.used / u.total * 100, 1), "low": free < cfg["disk_min_free_gb"]})
        if free < cfg["disk_min_free_gb"]:
            alerts.append(f"[磁碟快滿] {root} 只剩 {free:.1f}GB（門檻 {cfg['disk_min_free_gb']}GB）")

    return {"schema": 2, "generated_at": _iso(now), "ok": not alerts, "alert_count": len(alerts),
            "alerts": alerts, "projects": projects, "outputs": outputs, "disks": disks}


def format_alerts(report: dict) -> str:
    return "\n".join([f"⚠️ 健康巡檢發現 {report['alert_count']} 項異常（{report['generated_at']}）"]
                     + [f"• {a}" for a in report["alerts"]])


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg_path = Path(argv[argv.index("--config") + 1]) if "--config" in argv else HERE / "config.json"
    if not cfg_path.exists():
        print(f"找不到設定檔 {cfg_path}。請把 config.example.json 複製成 config.json 再改路徑。")
        return 2
    cfg = load_config(cfg_path)
    data_dir = Path(cfg.get("data_dir") or HERE / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    state = data_dir / "state.json"
    prev = None
    if state.exists():
        try:
            prev = json.loads(state.read_text(encoding="utf-8"))
        except ValueError:
            prev = None
    report = run(cfg, prev=prev)
    tmp = state.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, state)
    (data_dir / "last_run.txt").write_text(
        f"{report['generated_at']} ok={report['ok']} alerts={report['alert_count']}\n"
        + ("\n".join(report["alerts"]) + "\n" if report["alerts"] else ""), encoding="utf-8")

    if "--report" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["ok"]:
        return 0
    text = format_alerts(report)
    if "--report" not in argv:
        print(text)
    if "--no-notify" not in argv:
        import notify
        errors = []
        channels = notify.send(text, cfg.get("notify", {}), errors=errors)
        print(f"通知管道：{', '.join(channels) or '（沒有送出任何通知）'}")
        if errors:
            print("通知失敗：" + "；".join(errors))
        with (data_dir / "last_run.txt").open("a", encoding="utf-8") as f:  # 背景執行看不到畫面，結果也留一份
            f.write(f"通知管道：{', '.join(channels) or '無'}\n" + "".join(f"通知失敗 {e}\n" for e in errors))
    return 1


if __name__ == "__main__":
    if sys.stdout is None:  # pythonw 背景執行沒有主控台
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
