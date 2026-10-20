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
import threading
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
    "renotify_hours": 72,   # 同一個異常沒解決時，隔多久才再提醒一次（0＝每次巡檢都通知）
    "notify": {},
}


class ConfigError(ValueError):
    pass


def validate_config(cfg: dict) -> dict:
    """設定檔寫錯時給看得懂的訊息（不要丟 traceback，背景執行時根本看不到）。"""
    errs = []
    for sec in ("projects", "outputs"):
        items = cfg.get(sec)
        if not isinstance(items, list):
            errs.append(f"{sec} 必須是清單")
            continue
        for i, it in enumerate(items):
            if not isinstance(it, dict) or not isinstance(it.get("path"), str) or not it["path"].strip():
                errs.append(f"{sec}[{i}] 必須有文字的 path")
                continue
            it.setdefault("name", it["path"])
    for k in ("log_stale_hours", "db_max_mb", "log_max_mb", "disk_min_free_gb", "renotify_hours"):
        if isinstance(cfg.get(k), bool) or not isinstance(cfg.get(k), (int, float)):
            errs.append(f"{k} 必須是數字")
    for i, it in enumerate(cfg.get("outputs") or []):
        if isinstance(it, dict) and "max_age_hours" in it and not isinstance(it["max_age_hours"], (int, float)):
            errs.append(f"outputs[{i}].max_age_hours 必須是數字")
    if not isinstance(cfg.get("notify"), dict):
        errs.append("notify 必須是物件")
    if errs:
        raise ConfigError("設定檔有問題：" + "；".join(errs))
    return cfg


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            raise ConfigError(f"設定檔不是合法的 JSON（{e}）。Windows 路徑請用正斜線 / 或把反斜線寫兩次")
        if not isinstance(user, dict):
            raise ConfigError("設定檔最外層必須是 { } 物件")
        cfg.update(user)
    return validate_config(cfg)


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

    alerts, keys, projects = [], [], []

    def alert(key, msg):
        """key＝這個異常的固定身分（不含會變的數字），通知節流靠它判斷「是不是同一個異常」。"""
        keys.append(key)
        alerts.append(msg)

    for pj in cfg["projects"]:
        name, root = pj["name"], pj["path"]
        stale_h = pj.get("log_stale_hours", cfg["log_stale_hours"])
        rep = {"name": name, "path": root, "exists": os.path.isdir(root), "newest_log": None,
               "log_count": 0, "databases": [], "large_logs": []}
        projects.append(rep)
        if not rep["exists"]:
            alert(f"path:{name}", f"[路徑不見了] {name}：{root}")
            continue
        dbs, logs = scan_project(root, cfg)
        rep["log_count"] = len(logs)
        if logs:
            path, _s, mtime = max(logs, key=lambda t: t[2])
            age = (now - mtime) / 3600
            rep["newest_log"] = {"path": path, "mtime": _iso(mtime), "age_hours": round(age, 2), "stale": age > stale_h}
            if age > stale_h:
                alert(f"stale:{name}", f"[可能停了] {name}：最新 log 已 {age:.1f} 小時沒更新（門檻 {stale_h}h）→ {path}")
        elif pj.get("expect_logs", True):
            alert(f"nolog:{name}", f"[沒有 log] {name}：找不到任何 log，無法判斷是否在運作")
        for path, size, _m in sorted(dbs, key=lambda t: -t[1]):
            ok, logical, err = probe_db(path)
            entry = {"path": path, "size_mb": round(size / MB, 2), "openable": ok,
                     "logical_mb": round(logical / MB, 2) if logical is not None else None}
            if isinstance(prev_db.get(path), (int, float)):
                entry["delta_mb"] = round(entry["size_mb"] - prev_db[path], 2)
            if err:
                entry["error"] = err
                alert(f"dbopen:{path}", f"[DB 打不開] {name}：{os.path.basename(path)}（{err}）")
            if size / MB > cfg["db_max_mb"]:
                alert(f"dbbig:{path}", f"[DB 太大] {name}：{os.path.basename(path)} = {size / MB:.1f}MB（門檻 {cfg['db_max_mb']}MB）")
            rep["databases"].append(entry)
        for path, size, _m in logs:
            if size / MB > cfg["log_max_mb"]:
                rep["large_logs"].append({"path": path, "size_mb": round(size / MB, 2)})
                alert(f"logbig:{path}", f"[LOG 太大] {name}：{os.path.basename(path)} = {size / MB:.1f}MB（門檻 {cfg['log_max_mb']}MB）")

    outputs = []
    for it in cfg["outputs"]:
        maxh = float(it.get("max_age_hours", 48))
        m = newest_mtime(it["path"])
        entry = {"name": it["name"], "path": it["path"], "max_age_hours": maxh,
                 "mtime": _iso(m) if m else None, "age_hours": round((now - m) / 3600, 2) if m else None}
        outputs.append(entry)
        if m is None:
            alert(f"outmissing:{it['name']}", f"[產出不見了] {it['name']}：找不到 {it['path']}")
        elif (now - m) / 3600 > maxh:
            alert(f"outstale:{it['name']}", f"[沒有產出] {it['name']}：最後更新 {datetime.fromtimestamp(m):%Y-%m-%d %H:%M}"
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
            alert(f"disk:{root}", f"[磁碟快滿] {root} 只剩 {free:.1f}GB（門檻 {cfg['disk_min_free_gb']}GB）")

    return {"schema": 2, "generated_at": _iso(now), "ok": not alerts, "alert_count": len(alerts),
            "alerts": alerts, "alert_keys": keys, "projects": projects, "outputs": outputs, "disks": disks}


def format_alerts(report: dict) -> str:
    return "\n".join([f"⚠️ 健康巡檢發現 {report['alert_count']} 項異常（{report['generated_at']}）"]
                     + [f"• {a}" for a in report["alerts"]])


def _atomic_write(path: Path, text: str):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")   # 每個程序各用自己的暫存檔，同時跑兩個也不會寫壞
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def plan_notification(report: dict, notified: dict, now: float, renotify_hours: float):
    """通知節流：回傳 (要送的文字或 None, 要更新進 notified 的新狀態)。
    - 新出現的異常：馬上通知
    - 同一個異常還沒解決：每 renotify_hours 小時才再提醒一次（0＝每次都通知）
    - 之前通知過、現在好了：送一則「已恢復」
    送出成功之後才把回傳的新狀態存檔（送失敗就下次再試）。"""
    current = dict(zip(report.get("alert_keys", []), report["alerts"]))
    due = {k: t for k, t in current.items()
           if renotify_hours <= 0 or k not in notified or now - notified[k].get("last", 0) >= renotify_hours * 3600}
    recovered = [rec.get("text", k) for k, rec in notified.items() if k not in current]
    if not due and not recovered:
        return None, notified
    lines = []
    if due:
        lines.append(f"⚠️ 健康巡檢發現 {len(due)} 項異常（{report['generated_at']}）")
        lines += [f"• {t}" for t in due.values()]
        quiet = len(current) - len(due)
        if quiet:
            lines.append(f"（另有 {quiet} 項先前已通知、仍未解決，{renotify_hours:g} 小時內不重複提醒）")
    if recovered:
        lines.append(f"✅ 已恢復 {len(recovered)} 項：")
        lines += [f"• {t}" for t in recovered]
    new_state = {k: dict(rec, text=current[k]) for k, rec in notified.items() if k in current}
    for k, t in due.items():
        new_state[k] = {"last": now, "text": t}
    return "\n".join(lines), new_state


def _load_json(path: Path, default):
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, type(default)) else default
    except (OSError, ValueError):
        return default


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg_path = Path(argv[argv.index("--config") + 1]) if "--config" in argv else HERE / "config.json"
    if not cfg_path.exists():
        print(f"找不到設定檔 {cfg_path}。請把 config.example.json 複製成 config.json 再改路徑。")
        return 2
    try:
        cfg = load_config(cfg_path)
    except ConfigError as e:
        print(str(e))
        try:   # 背景執行看不到畫面，錯誤也留一份
            d = Path(HERE / "data")
            d.mkdir(parents=True, exist_ok=True)
            (d / "last_run.txt").write_text(f"{_iso(time.time())} {e}\n", encoding="utf-8")
        except OSError:
            pass
        return 2
    data_dir = Path(cfg.get("data_dir") or HERE / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    state = data_dir / "state.json"
    prev = _load_json(state, {}) or None
    report = run(cfg, prev=prev)
    _atomic_write(state, json.dumps(report, ensure_ascii=False, indent=2))
    _atomic_write(data_dir / "last_run.txt",
                  f"{report['generated_at']} ok={report['ok']} alerts={report['alert_count']}\n"
                  + ("\n".join(report["alerts"]) + "\n" if report["alerts"] else ""))

    if "--report" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"] and "--report" not in argv:
        print(format_alerts(report))
    if "--no-notify" not in argv:
        notified_file = data_dir / "notify_state.json"
        notified = _load_json(notified_file, {})
        text, new_state = plan_notification(report, notified, time.time(), float(cfg["renotify_hours"]))
        if text is None:
            if not report["ok"]:
                with (data_dir / "last_run.txt").open("a", encoding="utf-8") as f:
                    f.write("通知：同樣的異常已通知過，這次不重複提醒\n")
        else:
            import notify
            errors = []
            channels = notify.send(text, cfg.get("notify", {}), errors=errors)
            print(f"通知管道：{', '.join(channels) or '（沒有送出任何通知）'}")
            if errors:
                print("通知失敗：" + "；".join(errors))
            with (data_dir / "last_run.txt").open("a", encoding="utf-8") as f:  # 背景執行看不到畫面，結果也留一份
                f.write(f"通知管道：{', '.join(channels) or '無'}\n" + "".join(f"通知失敗 {e}\n" for e in errors))
            if channels:   # 至少一個管道送成功才算「已通知」；全失敗就下次再試
                _atomic_write(notified_file, json.dumps(new_state, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    if sys.stdout is None:  # pythonw 背景執行沒有主控台
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
