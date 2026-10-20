"""告警出口：Discord webhook、LINE 推播（Messaging API）。只用標準庫。

憑證一律從環境變數（或 .env）讀，設定檔只寫「要讀哪個環境變數」。
任何一個管道失敗都不會讓巡檢掛掉。
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_dotenv(path=HERE / ".env"):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _post(url, payload, headers=None, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return 200 <= r.status < 300


def discord(text, webhook_url, post=_post):
    return post(webhook_url, {"content": text[:1900]})


def line_push(text, token, to, post=_post):
    return post("https://api.line.me/v2/bot/message/push",
                {"to": to, "messages": [{"type": "text", "text": text[:4900]}]},
                {"Authorization": f"Bearer {token}"})


def send(text, conf: dict, post=_post, errors: list = None) -> list:
    """依設定送出，回傳成功的管道名稱。conf 例：
    {"discord_webhook_env": "DISCORD_WEBHOOK_URL", "line_token_env": "LINE_CHANNEL_ACCESS_TOKEN", "line_to_env": "LINE_TO"}
    errors：選用的 list，送失敗的管道會把「管道：原因」加進去（呼叫端可寫進 last_run.txt，背景執行時 stderr 看不到）。
    """
    errors = errors if errors is not None else []
    load_dotenv()
    sent = []
    hook = os.getenv(conf.get("discord_webhook_env", "DISCORD_WEBHOOK_URL"), "")
    if hook:
        try:
            if discord(text, hook, post):
                sent.append("discord")
            else:
                errors.append("discord：回應不是 2xx")
        except Exception as e:
            errors.append(f"discord：{e}")
            sys.stderr.write(f"[notify] Discord 失敗：{e}\n")
    token = os.getenv(conf.get("line_token_env", "LINE_CHANNEL_ACCESS_TOKEN"), "")
    to = os.getenv(conf.get("line_to_env", "LINE_TO"), "")
    if token and to:
        try:
            if line_push(text, token, to, post):
                sent.append("line")
            else:
                errors.append("line：回應不是 2xx")
        except Exception as e:
            errors.append(f"line：{e}")
            sys.stderr.write(f"[notify] LINE 失敗：{e}\n")
    return sent


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "[tw-health-monitor] 通知測試"
    print("送出管道：", send(msg, {}) or "（沒有設定任何管道）")
