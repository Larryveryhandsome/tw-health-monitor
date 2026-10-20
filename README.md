# tw-health-monitor｜自架服務每日健康巡檢

家裡或店裡那台電腦跑著 LINE bot、爬蟲、排程、備份……某天才發現**它已經停了一個禮拜**。
這個小工具每天幫你看一次，**沒事就安靜，有事才通知你**（Discord 或 LINE）。

- **純唯讀**：只看檔案的大小和時間；SQLite 用 `immutable` 唯讀模式打開，不寫入、不上鎖，不會干擾正在跑的服務。
- **沒事不吵**：健康就什麼都不輸出；每次結果都寫進 `data/state.json`，下次可以比較資料庫長大多少。
- **不洗版**：同一個異常沒解決時，預設每 72 小時（`renotify_hours`，0＝每次都通知）才提醒一次；新異常馬上通知，好了會推一則「已恢復」。狀態記在 `data/notify_state.json`。
- **通知失敗不會讓巡檢掛掉**：Discord／LINE 暫時性失敗（連線、429、5xx）會自動重試 3 次，仍失敗就只寫在 `data/last_run.txt`（網址與 token 會遮掉），下次巡檢再補送。
- 只用 Python 標準庫，不用裝任何套件。

## 檢查什麼

| # | 項目 | 判斷 | 設定 |
|---|---|---|---|
| 1 | 每個專案「最新的 log」多久沒更新 | 太久＝服務可能停了 | `log_stale_hours`（預設 26 小時，可每個專案各自設） |
| 2 | SQLite 資料庫 | 太大，或打不開（壞掉） | `db_max_mb`（預設 500MB） |
| 3 | 單一 log 檔大小 | 太大＝沒有輪替、會一直長 | `log_max_mb`（預設 50MB） |
| 4 | 磁碟剩餘空間 | 太少 | `disk_min_free_gb`（預設 10GB） |
| 5 | 應該定期更新的產出（報表、備份…） | 太久沒更新＝排程「顯示成功」其實沒產出 | `outputs[].max_age_hours` |

## 適合誰

- 自己架 LINE bot、Discord bot、爬蟲、自動化排程的**個人開發者、小店老闆**
- 有一台「一直開著」的 Windows／Mac／Linux 電腦或 NAS 的人
- 不想為了監控裝一整套 Prometheus／Grafana 的人

---

## 需要哪些金鑰？

**不設定也能用**：異常會寫在 `data/last_run.txt`。想在手機收到通知，再設定下面任一種（都免費）：

| 管道 | 要準備什麼 |
|---|---|
| Discord | 頻道的 Webhook 網址 |
| LINE | Messaging API 的 Channel access token＋你自己的 userId |

👉 **一步一步的申請與測試方法：[docs/金鑰申請.md](docs/金鑰申請.md)**。

---

## 怎麼用

**需要：** Python 3.9 以上。

1. 下載這個專案（綠色 **Code** 按鈕 → **Download ZIP**），解壓縮。
2. 把 `config.example.json` 複製成 **`config.json`**，把 `projects` 改成你要盯的服務資料夾、`outputs` 改成應該定期更新的檔案或資料夾。
3. （選用）把 `.env.example` 複製成 `.env`，填 Discord webhook 或 LINE 的 token。
4. 手動跑一次看看：

   ```bash
   python monitor.py --report --no-notify
   ```

   - `python monitor.py`：正式巡檢，健康就不輸出（結束代碼 0），有異常印出並通知（結束代碼 1）
   - `--report`：不管健康與否都印完整 JSON
   - `--no-notify`：只印、不推通知
   - `--config 路徑`：指定別的設定檔

### 每天自動跑

**Windows（不會跳出黑色視窗）**：在資料夾裡開 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1 -At 09:00
```

它會建立一個每天 09:00 的工作排程，用 `wscript` + `run_hidden.vbs` + `pythonw` 在背景執行，完全不會跳出視窗。想移除：

```powershell
Unregister-ScheduledTask -TaskName "tw-health-monitor" -Confirm:$false
```

**macOS／Linux**：用 cron，例如每天 9 點：

```bash
0 9 * * * cd /path/to/tw-health-monitor && /usr/bin/python3 monitor.py
```

### 通知設定

| 管道 | 怎麼拿 | `.env` |
|---|---|---|
| Discord | 頻道設定 → 整合 → Webhook → 複製網址 | `DISCORD_WEBHOOK_URL` |
| LINE | [LINE Developers](https://developers.line.biz/console/) 建 Messaging API channel，取 access token；`LINE_TO` 是你自己的 userId | `LINE_CHANNEL_ACCESS_TOKEN`、`LINE_TO` |

設定檔 `config.json` 裡的 `notify` 只寫「要讀哪個環境變數」，**憑證不寫進設定檔**。測試通知：

```bash
python notify.py "測試一下"
```

---

## 設定檔範例

```json
{
  "log_stale_hours": 26,
  "renotify_hours": 72,
  "projects": [
    {"name": "客服 bot", "path": "C:/services/my-bot"},
    {"name": "每小時排程", "path": "C:/services/hourly", "log_stale_hours": 3},
    {"name": "不寫 log 的服務", "path": "C:/services/quiet", "expect_logs": false}
  ],
  "outputs": [
    {"name": "每日報表", "path": "C:/services/reports/daily.html", "max_age_hours": 36},
    {"name": "備份資料夾", "path": "D:/backup", "max_age_hours": 48}
  ]
}
```

設定檔寫錯（JSON 格式、缺 `path`、數字欄位填文字）會直接告訴你哪裡有問題，結束代碼 2。`outputs` 的路徑可以是檔案，也可以是資料夾（看裡面最新的一個檔）。Windows 路徑請用正斜線 `/`。

## 給開發者

| 檔案 | 用途 |
|---|---|
| `monitor.py` | 巡檢主程式（`run(cfg)` 回傳報告，方便測試） |
| `notify.py` | Discord／LINE 通知 |
| `run_hidden.vbs`、`install_task.ps1` | Windows 背景執行與工作排程 |

```bash
python -m unittest discover -s tests
```

## 授權

[Apache License 2.0](LICENSE)

## 🛠️ 需要客製化？

這個工具是開源的，可以免費使用。如果你想要：
- 改成符合你們公司的流程、欄位或品牌
- 串接你現在在用的系統（LINE、Google 表單、POS、會計軟體……）
- 幫你安裝、架站、教學，或長期維護

歡迎加 LINE 跟我們聊。我們會依需求的範圍與複雜度報價，原則上以實際報價單為準。

<a href="https://lin.ee/pdYGARu"><img src="https://scdn.line-apps.com/n/line_add_friends/btn/zh-Hant.png" alt="加入好友" height="36"></a>

- 💬 LINE 官方帳號：[@812ibtir](https://lin.ee/pdYGARu)（請註明「健康巡檢 客製化」）
- 🏢 留白事務所：網頁設計、AI 自動化、房仲遠端助理
- 👤 作者：[林其衞 Larry](https://github.com/Larryveryhandsome)
