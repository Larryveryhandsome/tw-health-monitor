"""告警出口：Discord webhook、LINE 推播（Messaging API）。只用標準庫。

憑證一律從環境變數（或 .env）讀，設定檔只寫「要讀哪個環境變數」。
任何一個管道失敗都不會讓巡檢掛掉。

- 暫時性失敗（連線錯誤、HTTP 429、5xx）會退避重試最多 3 次；4xx（網址／token 錯）不重試。
  註：讀取逾時時對方可能其實已收到，重試偶爾會造成同一則訊息出現兩次（寧可重複不要漏報）。
- 訊息太長會自動分成多則（Discord 每則 1900 字、LINE 4900 字，最多 3 則），不會被默默截掉。
- 錯誤訊息會把 webhook 網址、token 遮掉再寫進 last_run.txt／stderr。
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
MAX_CHUNKS = 3
RETRIES = 3


def load_dotenv(path=HERE / ".env"):
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _retry_after(e, attempt):
    try:
        ra = e.headers.get("Retry-After") if e.headers else None
        if ra is None:
            ra = json.loads(e.read().decode("utf-8", "replace")).get("retry_after")
        return min(max(float(ra), 0.5), 30.0)
    except Exception:
        return 2.0 ** attempt


def _post(url, payload, headers=None, timeout=10, retries=RETRIES, sleep=time.sleep):
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        last = attempt == retries - 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and not last:
                sleep(_retry_after(e, attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if last:
                raise
            sleep(2.0 ** attempt)
    return False


def check_url(url: str):
    """webhook 只接受 https（本機 http://127.0.0.1／localhost 測試用除外）；擋掉 file:// 等奇怪的協定。"""
    u = urlparse(url)
    if u.scheme == "https" and u.netloc:
        return
    if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"):
        return
    raise ValueError("webhook 網址必須是 https:// 開頭")


def redact(msg, *secrets) -> str:
    """把 webhook 網址、token 等憑證從訊息裡遮掉。"""
    msg = str(msg)
    for s in secrets:
        if s and len(s) >= 4:
            msg = msg.replace(s, "***")
    msg = re.sub(r"/api/webhooks/\d+/[\w-]+", "/api/webhooks/***", msg)
    return re.sub(r"Bearer\s+[\w.\-]+", "Bearer ***", msg)


def chunk_text(text: str, limit: int, max_chunks: int = MAX_CHUNKS):
    """依行切成不超過 limit 字的幾則；超過 max_chunks 則的部分用一行說明取代。"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:   # 單行就超長：硬切
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + (1 if cur else 0) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur or not chunks:
        chunks.append(cur)
    if len(chunks) > max_chunks:
        hidden = len(chunks) - max_chunks
        chunks = chunks[:max_chunks]
        note = f"\n…（還有 {hidden} 則沒顯示，完整內容見 data/last_run.txt）"
        chunks[-1] = chunks[-1][:limit - len(note)] + note
    return chunks


def discord(text, webhook_url, post=_post):
    check_url(webhook_url)
    ok = True
    for c in chunk_text(text, 1900):
        # allowed_mentions 清空：檔名／服務名稱裡的 @everyone 不會真的 tag 全頻道
        ok = post(webhook_url, {"content": c, "allowed_mentions": {"parse": []}}) and ok
    return ok


def line_push(text, token, to, post=_post):
    ok = True
    for c in chunk_text(text, 4900):
        ok = post("https://api.line.me/v2/bot/message/push",
                  {"to": to, "messages": [{"type": "text", "text": c}]},
                  {"Authorization": f"Bearer {token}"}) and ok
    return ok


def send(text, conf: dict, post=_post, errors: list = None) -> list:
    """依設定送出，回傳成功的管道名稱。conf 例：
    {"discord_webhook_env": "DISCORD_WEBHOOK_URL", "line_token_env": "LINE_CHANNEL_ACCESS_TOKEN", "line_to_env": "LINE_TO"}
    errors：選用的 list，送失敗的管道會把「管道：原因」加進去（呼叫端可寫進 last_run.txt，背景執行時 stderr 看不到）。
    """
    errors = errors if errors is not None else []
    load_dotenv()
    sent = []
    hook = os.getenv(conf.get("discord_webhook_env", "DISCORD_WEBHOOK_URL"), "")
    token = os.getenv(conf.get("line_token_env", "LINE_CHANNEL_ACCESS_TOKEN"), "")
    to = os.getenv(conf.get("line_to_env", "LINE_TO"), "")
    if hook:
        try:
            if discord(text, hook, post):
                sent.append("discord")
            else:
                errors.append("discord：回應不是 2xx")
        except Exception as e:
            m = redact(f"{type(e).__name__}: {e}", hook, token, to)
            errors.append(f"discord：{m}")
            sys.stderr.write(f"[notify] Discord 失敗：{m}\n")
    if token and to:
        try:
            if line_push(text, token, to, post):
                sent.append("line")
            else:
                errors.append("line：回應不是 2xx")
        except Exception as e:
            m = redact(f"{type(e).__name__}: {e}", hook, token, to)
            errors.append(f"line：{m}")
            sys.stderr.write(f"[notify] LINE 失敗：{m}\n")
    return sent


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "[tw-health-monitor] 通知測試"
    print("送出管道：", send(msg, {}) or "（沒有設定任何管道）")
