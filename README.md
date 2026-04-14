# net-grading

學生互評同步閘道：一次評分，自動分發到 Site1 (ita-grading) / Site2 (ntust-grading, Firebase) / Site3 (Google Apps Script)。

詳細設計見 [docs/superpowers/specs/2026-04-14-net-grading-sync-design.md](docs/superpowers/specs/2026-04-14-net-grading-sync-design.md)。

## 啟動

```bash
# 一次性
uv sync
cp .env.example .env
# 編輯 .env，填入：
#   SESSION_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
#   SITE2_ENC_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
uv run alembic upgrade head

# 開發模式
uv run uvicorn net_grading.app:app --reload --host 127.0.0.1 --port 8080

# 正式環境（前置 Cloudflare + nginx）
uv run uvicorn net_grading.app:app --host 127.0.0.1 --port 8080
```

## 路由

| Path | 說明 |
|---|---|
| `/` | 依登入狀態重導 `/dashboard` 或 `/login` |
| `/login` | Site1 學號登入（僅學生）|
| `/logout` | 登出 |
| `/dashboard?period=midterm\|final` | 被評學生列表；首次進站自動匯入 Site1/Site2 |
| `/grade/{period}/{target_id}` | 評分表單（數字 input + Tab/↑↓/Enter + wheel-lock）+ 三站同步狀態 + 歷史 |
| `/settings` | 主題、Site2 憑證管理 |
| `/settings/site2` | Site2（Firebase）連線設定 |
| `/conflicts` | 首次匯入衝突解決 |
| `/sync/{submission_id}/retry/{site}` | 單站重試 |
| `/health` | 健康檢查 |

## 里程碑

- **M1** FastAPI 骨架 + SQLite 六張表 + alembic migration
- **M2** Site1 client + 登入流程 + session middleware
- **M3** Dashboard 目標列表 + grade 表單 + 本地送出
- **M4** Site2 + Site3 client + 憑證設定頁
- **M5** 並行同步 orchestrator（阻塞 best-effort，~3s）+ 單站重試
- **M6** 首次匯入 + 衝突偵測 + 解衝突 UI
- **M7** README（視覺 polish 與 SSE 實時推送待後續 session）

## 未完成 / 後續

- Tailwind 正式樣式（目前 M2 過度期的 inline CSS）
- SSE 即時同步進度推送（目前送出阻塞 2-3s 後一次呈現）
- Dashboard 每位被評學生行的三站 pill
- 期別 `is_open=0` 的 server-side 阻擋（目前僅 UI 鎖灰）
