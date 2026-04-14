# scoring-sync：學生互評同步閘道設計

**日期**：2026-04-14
**狀態**：設計定稿，待 review → 進實作計畫

---

## 0. 總覽

全班約 27 位學生需對彼此互評，但已有四位同學各自建了評分網站，使用者重複填四次不實際。本系統（scoring-sync）作為「第四個」網站，提供單一入口、隱藏同步細節：

- 使用者在本站完成一次送出
- 系統並行同步到其他三站（Site1 / Site2 / Site3）
- 本地 SQLite 持久化所有送出紀錄（即使不開啟同步也可獨立使用）
- 視覺化三站同步狀態、偵測並協助解決跨站資料不一致

**技術疊層**：FastAPI（async）+ Jinja2 + HTMX + Alpine.js + Tailwind CSS + SQLAlchemy async + aiosqlite + httpx[http2]。單進程，Cloudflare + nginx 由使用者外層處理。

---

## 1. 範圍 & 非範圍

### 目標（In Scope）
1. 期中 / 期末互評的評分編輯與送出
2. 自動轉譯 + 同步送出到 Site1、Site2、Site3 共三站
3. 本地 SQLite 作為主檔（append-only submission 版本史）
4. 第一次使用時從 Site2（優先）/ Site1 拉取既有資料並偵測衝突
5. Site2 憑證保存（可選「記住我」）與自動 idToken 續期
6. 多使用者雲端共用部署；每位使用者資料嚴格隔離
7. 預設 dark mode，可切 light mode

### 非目標（Out of Scope）
- 寫測試（本輪明確省略）
- Site3 資料讀取（Apps Script 僅暴露 doPost）
- 客製化評分配分（直接採 Site1/3 現行 30/30/20/10/10 = 100 分制）
- 手機原生 App（僅 responsive web）
- CSV 匯出、離線模式（Service Worker）、通知推播

---

## 2. 關鍵決策彙總

| # | 主題 | 決策 |
|---|---|---|
| 1 | 部署型態 | 雲端多使用者共用（使用者自管伺服器，外層 Cloudflare + nginx）|
| 2 | 前端技術 | FastAPI + Jinja2 + HTMX + Alpine.js + Tailwind CSS |
| 3 | 本站認證 | 直接轉接 Site1 學號登入；`need_password=true` 時再跳密碼欄位 |
| 4 | Site2 憑證 | 混合模式：預設只存 server-side session；勾「記住我」才 Fernet 加密 refreshToken 入 DB；不存 password |
| 5 | Site3 讀取 | 純寫入鏡像；衝突比對只跨 Site1 / Site2 / 本地 |
| 6 | 衝突粒度 | 每位被評學生獨立決定採用哪個來源版本 |
| 7 | 送出流程 | 阻塞並行 best-effort；三站請求並行，結果經 SSE 即時回推 UI；失敗單站可重試 |

### 次要預設
- **期別關閉**（Site1 `is_open=0`）：整個 tab 鎖灰，不允許本地草稿編輯
- **版本歷史**：Grade 頁下方折疊區顯示本地所有 submissions，可開 read-only snapshot
- **Migration**：alembic（未來擴欄位友善）
- **Tailwind**：CLI 編譯到 `static/css/app.css`

---

## 3. 整體架構

### 3.1 分層

```
┌─────────────────────────────────────────┐
│  routes/      (FastAPI endpoints, UI)   │  ← HTTP 殼、渲染 Jinja、HTMX/SSE response
├─────────────────────────────────────────┤
│  sync/        (Orchestrator, Conflict)  │  ← 商業邏輯：pull / push / diff
├─────────────────────────────────────────┤
│  sites/       (Site1/2/3 Adapter)       │  ← 三站協議差異完全隔離在這層
└─────────────────────────────────────────┘
         ↓                    ↓
      db/ (SQLite)       httpx (outbound)
```

### 3.2 Adapter 統一介面（`sites/base.py`）

```python
from typing import Protocol
class SiteClient(Protocol):
    async def login(self, creds: Credentials) -> Session: ...
    async def list_targets(self, session, period) -> list[Target]: ...
    async def fetch_submission(self, session, period, target_id) -> Submission | None: ...
    async def submit(self, session, period, target_id, scores, comment, self_note) -> SubmitResult: ...
```

- `Site3Client.login` 回 `NullSession`（不需登入）
- `Site3Client.list_targets / fetch_submission` 丟 `NotSupported`（寫入鏡像模式）
- `Site2Client.login` 處理 Firebase idToken + refreshToken；送出前自動續期

### 3.3 資料流示意（送出）

```
Browser ──HTTPS──▶ Nginx ──▶ FastAPI
                               │
                               ├─▶ auth: 驗 cookie → request.state.site1_sid, user_id
                               ├─▶ INSERT submissions(source='local', ...)
                               ├─▶ asyncio.create_task(run_sync(submission_id))
                               │     ├─▶ asyncio.gather(
                               │     │     Site1Client.submit(),
                               │     │     Site2Client.submit(),   ← 先 refresh idToken
                               │     │     Site3Client.submit()
                               │     │   )
                               │     ├─▶ UPDATE sync_logs per site
                               │     └─▶ sse.publish(submission_id, events)
                               └─▶ 返回 HTMX fragment（SSE consumer 掛載）

同時間：
Browser (hx-ext="sse" sse-connect=/sync/.../events) ──▶ FastAPI SSE stream
                                                         └─ asyncio.Queue per submission_id
```

---

## 4. 資料模型

**全部六張表**，user_id（= Site1 學號）為 per-user 隔離主軸。**submissions 採 append-only** 對齊 Site1 的 version history 模型。

### 4.1 `users`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `student_id` | TEXT PK | Site1 `actor_id` |
| `name` | TEXT | Site1 登入回傳 |
| `class_name` | TEXT | Site1 登入回傳 |
| `created_at` | DATETIME | |
| `last_login_at` | DATETIME | |

### 4.2 `sessions`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | TEXT PK | `secrets.token_urlsafe(32)`；Set-Cookie 的值 |
| `user_id` | TEXT FK | → users |
| `site1_sid_enc` | BLOB | Fernet 加密的 Site1 sid cookie |
| `site1_sid_expires_at` | DATETIME | Site1 原始過期時間 |
| `expires_at` | DATETIME | 本站 session 過期（與上同步）|
| `created_at` | DATETIME | |

### 4.3 `site2_credentials`（僅「記住我」使用者）

| 欄位 | 型別 | 說明 |
|---|---|---|
| `user_id` | TEXT PK FK | 一人一筆 |
| `email` | TEXT | 明文（UI 顯示用）|
| `enc_refresh_token` | BLOB | Fernet 加密 |
| `id_token` | TEXT | 短效快取（1h）|
| `id_token_expires_at` | DATETIME | |
| `local_id` | TEXT | Firebase UID |
| `created_at` | DATETIME | |
| `updated_at` | DATETIME | |

**不存 password**（安全邊界：DB 外洩也拿不到使用者 Firebase 密碼）。

### 4.4 `submissions`（append-only）

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | 本地自增 |
| `user_id` | TEXT FK | 評分者 |
| `period` | TEXT | `midterm` / `final` |
| `target_student_id` | TEXT | 被評者學號 |
| `score_topic` | INT | 0–30 |
| `score_content` | INT | 0–30 |
| `score_narrative` | INT | 0–20 |
| `score_presentation` | INT | 0–10 |
| `score_teamwork` | INT | 0–10 |
| `total` | INT | 反正規化：上述五欄之和 |
| `comment` | TEXT | 評語（對他人可見，Site1/2/3 皆會同步）|
| `self_note` | TEXT | 自備備註（僅本地 + Site1；Site2/3 不含此欄）|
| `source` | TEXT | `local` / `imported_site1` / `imported_site2` |
| `submitted_at` | DATETIME | 本地時間戳 |

**Index**：`(user_id, period, target_student_id, submitted_at DESC)` — 查「最新版」即 `ORDER BY submitted_at DESC LIMIT 1`

### 4.5 `sync_logs`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `submission_id` | INTEGER FK | → submissions |
| `site` | TEXT | `site1` / `site2` / `site3` |
| `status` | TEXT | `pending` / `success` / `failed` / `skipped` |
| `http_status` | INT NULL | |
| `response_body` | TEXT NULL | 緊湊 JSON（上限 ~4KB）|
| `error_message` | TEXT NULL | |
| `external_id` | TEXT NULL | Site1 submission id / Site3 row / Site2 doc path |
| `attempted_at` | DATETIME | |
| `duration_ms` | INT | |

**Index**：`(submission_id)`、`(site, status)`（供全站失敗統計）

### 4.6 `targets_cache`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `user_id, period, target_student_id` | — | 複合 PK |
| `name` | TEXT | |
| `class_name` | TEXT | |
| `is_self` | INT | 1 / 0，自評 high-light |
| `updated_at` | DATETIME | 每次 dashboard 重整 upsert |

### 4.7 `conflict_events`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | TEXT FK | |
| `period` | TEXT | |
| `target_student_id` | TEXT | |
| `site1_snapshot` | TEXT | JSON 字串，完整保留 Site1 當時內容 |
| `site2_snapshot` | TEXT | 同上 |
| `resolution` | TEXT NULL | `site1` / `site2` / `skip` / NULL（未決）|
| `resolved_at` | DATETIME NULL | |
| `created_at` | DATETIME | |

---

## 5. 認證流程

### 5.1 Flow A：本站登入（Site1 pass-through）

```
[POST /login {student_id}]
    │
    ▼
Site1Client.identify(student_id)
    Response: Set-Cookie sid=...; body: {actor_id, name, class_name, periods, need_password}
    │
    ├─ need_password=true → 渲染 /login?step=password → 使用者輸密碼（Site1 密碼端點；PoC 未展示，實作時向 Site1 作者確認）
    │
    └─ need_password=false → 直接進下一步
    │
    ▼
UPSERT users(student_id, name, class_name, last_login_at=now)
INSERT sessions(id=<token>, user_id, site1_sid_enc=Fernet(sid), expires_at=site1_expires)
Set-Cookie: ng_session=<id>; HttpOnly; Secure; SameSite=Lax; Max-Age=86399
302 /dashboard
```

**Middleware `require_session`**（FastAPI Depends）：每個受保護 route：
1. 從 cookie 取 `ng_session`
2. `SELECT sessions WHERE id=? AND expires_at > now()`；找不到 → 302 `/login`
3. 解密 `site1_sid_enc` → `request.state.site1_sid`
4. 若 `expires_at <= now() + 5min` → response header 塞 `HX-Trigger: session-expiring-soon`，前端彈 toast 提示重登

### 5.2 Flow B：Site2 憑證設定（`/settings/site2`）

```
[POST /settings/site2 {email, password, remember}]
    │
    ▼
Site2Client.login(email, password)
    POST identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=<SITE2_FIREBASE_API_KEY>
    Response: {idToken, refreshToken, localId, expiresIn}
    │
    ├─ 4xx EMAIL_NOT_FOUND / INVALID_PASSWORD → 錯誤提示，不寫 DB
    │
    ├─ 成功 + remember=true
    │     └─ UPSERT site2_credentials(
    │            enc_refresh_token=Fernet(refreshToken),
    │            id_token, id_token_expires_at=now+expiresIn,
    │            local_id, email)
    │
    └─ 成功 + remember=false
          └─ 寫入 server-side session dict（in-memory，keyed by ng_session id）
          └─ 關瀏覽器 / session 過期即清除
```

**記住我對比**：

| 情境 | idToken | refreshToken | 儲存位置 |
|---|---|---|---|
| `remember=false` | 1h 過期即失效 | 不保存 | in-memory session dict |
| `remember=true` | 背景 refresh 續命 | Fernet 加密存 DB | `site2_credentials` |

### 5.3 Flow C：Site2 idToken 自動續期

**背景 worker**（`sync/refresh_worker.py`，app lifespan 啟動）每 5 分鐘掃：

```python
SELECT * FROM site2_credentials WHERE id_token_expires_at < now() + 10min
for cred in due:
    refresh_token = Fernet.decrypt(cred.enc_refresh_token)
    POST securetoken.googleapis.com/v1/token?key=...  grant_type=refresh_token
    if 200:
        UPDATE id_token, id_token_expires_at=now+3600, enc_refresh_token=新 refreshToken
    elif 400 TOKEN_EXPIRED / USER_DISABLED:
        DELETE site2_credentials WHERE user_id=?
        # 下次進站使用者會看到 banner：「Site2 需要重新連線」
```

**送出時懶加載續期**（避免 race）：

```python
async def get_site2_id_token(user_id) -> str | None:
    cred = await db.get_site2_credentials(user_id)
    if not cred:
        return None
    if cred.id_token_expires_at < now() + 60:
        await refresh_site2_token(cred)   # 立即 refresh
    return cred.id_token
```

### 5.4 Flow D：登出

`POST /logout`：
1. `DELETE FROM sessions WHERE id=?`
2. **不**刪 `site2_credentials`（使用者只是登出本站，不是撤銷 Site2 授權）
3. Site1 sid 不主動 invalidate（Site1 無 logout API，自然 86399s 過期）
4. Cookie 清除 → 302 `/login`

另一按鈕 `/settings/site2/revoke`：明確 `DELETE site2_credentials`。

### 5.5 安全邊界

| 資產 | 儲存形式 | 鑰匙 | 洩漏衝擊 |
|---|---|---|---|
| 本站 session token | DB 明文 + HttpOnly cookie | — | DB 外洩 ≈ 線上 session 奪用；緩解：短壽命 + 到期強制重登 |
| Site1 sid | Fernet 加密 | `SESSION_SECRET` env | DB 外洩但 env 未洩：安全 |
| Site2 refreshToken | Fernet 加密 | `SITE2_ENC_KEY` env | 同上 |
| Site2 password | **不儲存** | — | DB 外洩也拿不到密碼 |

---

## 6. 同步引擎

### 6.1 首次拉取與匯入（`sync/pull.py`）

使用者完成 Site1 登入 + 可選 Site2 設定後第一次進 `/dashboard`：

```python
if COUNT(submissions WHERE user_id=?) > 0:
    skip import   # 已有本地紀錄

site1_grades, site2_grades = await asyncio.gather(
    Site1Client.list_submissions(...),            # 先 /api/student/targets，再對 evaluated=true 並行打 detail
    Site2Client.list_submissions(...) if site2_logged_in else return_none
)

for target in site1_targets:
    s1 = site1_grades.get(target.id)
    s2 = site2_grades.get(target.id)

    match (s1, s2):
        case (None, None):
            pass  # dashboard 顯示「未評」
        case (s, None) | (None, s):
            INSERT submissions(source=f'imported_site{n}', ...)
        case (s1, s2) if scores_equal(s1, s2) and comment_equal(s1, s2):
            INSERT submissions(source='imported_site2', ...)  # 兩邊相同，採 Site2
        case (s1, s2):
            INSERT conflict_events(site1_snapshot=s1, site2_snapshot=s2, resolution=NULL)
```

**效能**：Site1 detail 並行 27 筆請求，用 `httpx.AsyncClient` + `asyncio.Semaphore(10)` 限 10 並發避免被 rate-limit。

**比對函式**：

```python
def scores_equal(a, b) -> bool:
    return all(getattr(a, f) == getattr(b, f) for f in (
        "score_topic", "score_content", "score_narrative",
        "score_presentation", "score_teamwork"
    ))
```

### 6.2 衝突解決 UI（`/conflicts`）

**Dashboard 進站檢查**：

```
pending = SELECT conflict_events WHERE user_id=? AND resolution IS NULL
if pending:
    顯示頂部 banner「偵測到 N 筆不一致紀錄」+ CTA → /conflicts
    disable 「送出」按鈕直到全解
```

**衝突頁結構**（每筆衝突一個 card）：

```
┌──────────────────────────────────────────────────────┐
│  衝突解決（3 筆待處理）                               │
│  ────────────────────────────────────────────────   │
│  B11315009 黃宥維（四資工二甲）                       │
│  ┌──────────────┬──────────────┐                     │
│  │ Site1 版本    │ Site2 版本    │                     │
│  │ 總分 95       │ 總分 100      │                     │
│  │ 主題 30       │ 主題 30       │                     │
│  │ 內容 25  ⚠   │ 內容 30  ⚠   │                     │
│  │ ...           │ ...           │                     │
│  │ 送出時間      │ 送出時間      │                     │
│  │ 2026-04-13... │ 2026-04-14... │                     │
│  │ 評語：         │ 評語：         │                     │
│  │ (空)          │ 「表現優異」   │                     │
│  │               │               │                     │
│  │ [採用此版本]  │ [採用此版本]  │                     │
│  └──────────────┴──────────────┘                     │
│  [跳過這筆]                                           │
│                                                       │
│  ...                                                   │
│                                                       │
│  [全部採用 Site1]  [全部採用 Site2]                   │
└──────────────────────────────────────────────────────┘
```

**POST `/conflicts/{id}/resolve` 行為**：

```python
if choice == "skip":
    UPDATE conflict_events SET resolution='skip', resolved_at=now()
else:
    snapshot = c.site1_snapshot if choice == "site1" else c.site2_snapshot
    INSERT submissions(source=f'imported_{choice}', submitted_at=snapshot.timestamp, ...)
    UPDATE conflict_events SET resolution=choice, resolved_at=now()
# HTMX return OOB swap：該筆從衝突列移除
```

### 6.3 送出 & 平行同步（`sync/orchestrator.py`）

```
POST /grade/{period}/{target}/submit
    │
    ▼
INSERT submissions(status='pending', source='local', ...) → submission_id
    │
    ▼
HTMX response 立即返回「送出進度」片段 + SSE URL /sync/{submission_id}/events
    │
    ▼ asyncio.create_task(run_sync(submission_id))
run_sync:
    tasks = {
        "site1": Site1Client.submit(...),
        "site2": Site2Client.submit(...) if site2_ok else skip(),
        "site3": Site3Client.submit(...),
    }
    for name, _ in tasks: INSERT sync_logs(site=name, status='pending')

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    for (name, _), result in zip(tasks.items(), results):
        if isinstance(result, Exception):
            UPDATE sync_logs SET status='failed', error_message=str(result)
            sse_publish(event=name, data={"status": "failed", ...})
        else:
            UPDATE sync_logs SET status='success', external_id=result.id
            sse_publish(event=name, data={"status": "success", ...})
    sse_publish(event="done")
```

**前端 SSE 消費**：

```html
<div hx-ext="sse" sse-connect="/sync/{{submission_id}}/events"
     sse-swap="site1,site2,site3,done">
    <div id="site1-status">⏳ Site1 進行中...</div>
    <div id="site2-status">⏳ Site2 進行中...</div>
    <div id="site3-status">⏳ Site3 進行中...</div>
</div>
```

**SSE 實作**：`sse-starlette.EventSourceResponse` + `asyncio.Queue` per submission_id（記憶體 pub/sub）。使用者關 SSE 連線後 task 繼續；重開頁面時從 `sync_logs` 讀回最新狀態重現。

### 6.4 單站重試

```
POST /sync/{submission_id}/retry/{site}
    │
    ▼ 驗證最近一筆 sync_logs.status='failed'
    ▼ 重打該站（INSERT 新的 sync_logs 列）
    ▼ HTMX return 新狀態片段（OOB swap）
```

**上限**：同 `(submission_id, site)` 最多 5 次重試；超過按鈕 disable + 提示「請重新送出一筆」。

### 6.5 特殊情境

| 情境 | 行為 |
|---|---|
| Site2 未登入（session-only 未填過）| `sync_logs.status='skipped'` + UI 灰色 tag「Site2 未授權」+ CTA 去 `/settings/site2` |
| 期別 `is_open=0` | UI tab 鎖灰；強打 API 預期 Site1 也拒絕 |
| Site1 sid 過期 | 回 401 → orchestrator 攔截 → UI 跳重登入 modal；重登後自動重試這筆 |
| Site2 idToken 過期 | `get_site2_id_token()` 懶加載續期，使用者無感 |
| Site3 Apps Script 逾時 | httpx `timeout=10s`；進重試佇列，不阻擋其他兩站 |

---

## 7. UI 設計

### 7.1 Sitemap

```
/                          → redirect /dashboard 或 /login
/login                     → 學號輸入；need_password 二階段
/logout                    → POST 後 redirect /login
/dashboard                 → 期中/期末 tab + 被評學生列表 + 每位 sync pill
/grade/{period}/{target}   → 評分表單 + 歷史折疊
/grade/{period}/{target}/history  → read-only snapshot 列表
/conflicts                 → 首次匯入衝突強制入口
/settings                  → dark toggle、登出
/settings/site2            → Site2 email/password + 記住我 + 撤銷
/settings/site2/revoke     → POST，明確撤銷記住我
/sync/{submission_id}/events  → SSE 端點
/sync/{submission_id}/retry/{site}  → 單站重試
```

### 7.2 視覺方向（anti-template）

- 語氣：Linear + Raycast 之間，資料密度高、留白節奏清楚
- 標題：大對比字級（dashboard 頂 60px + subtitle 14px 灰）
- 資料：等寬字體（JetBrains Mono / IBM Plex Mono）顯示分數、學號、時間
- 同步狀態：彩色 pill（Site1/2/3 三顆並排，成功綠、失敗紅、進行琥珀、跳過灰）
- 狀態動畫：進行中 `animate-pulse`；完成 `scale` 動畫 300ms
- 留白：`clamp(3rem, 4vw, 6rem)` section gap
- 深色模式無陰影氾濫：subtle border + 漸層底色取代陰影

### 7.3 Design Tokens（`static/css/tokens.css`）

```css
:root {
    --bg-base:    oklch(98% 0 0);
    --bg-raised:  oklch(100% 0 0);
    --fg-primary: oklch(18% 0 0);
    --fg-muted:   oklch(45% 0 0);
    --accent:     oklch(55% 0.19 260);
    --success:    oklch(62% 0.15 145);
    --warning:    oklch(72% 0.17 70);
    --danger:     oklch(58% 0.22 25);
    --border:     oklch(90% 0 0);
}

.dark {
    --bg-base:    oklch(14% 0 0);   /* 非純黑，偏灰 */
    --bg-raised:  oklch(18% 0 0);
    --fg-primary: oklch(96% 0 0);
    --fg-muted:   oklch(65% 0 0);
    --accent:     oklch(72% 0.17 260);
    --success:    oklch(72% 0.17 145);
    --warning:    oklch(78% 0.17 70);
    --danger:     oklch(68% 0.22 25);
    --border:     oklch(25% 0 0);
}
```

### 7.4 主題切換（無 FOUC）

```html
<!-- 置於 <head> 最上，inline 阻塞執行 -->
<script>
    (function() {
        const t = localStorage.getItem('theme') || 'dark';
        document.documentElement.className = t;
    })();
</script>
```

```html
<body x-data="{ theme: localStorage.getItem('theme') || 'dark' }"
      x-init="$watch('theme', v => { localStorage.setItem('theme', v); document.documentElement.className = v })">
    <button @click="theme = theme === 'dark' ? 'light' : 'dark'">
        <span x-show="theme === 'dark'">🌙</span>
        <span x-show="theme === 'light'">☀️</span>
    </button>
```

### 7.5 關鍵頁面

**Dashboard**：

```
┌─────────────────────────────────────────────────────────────┐
│  期中報告互評             B11315009 黃宥維  [☀/🌙][設定][登出] │
│  ─────                                                       │
│  [ 期中 ] [ 期末 (未開放) ]                                   │
│                                                              │
│  27 位待評  ·  已評 12  ·  未評 15                           │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ B11307106  羅國豪  四資工二乙  │ 95/100 │ S1✓ S2✓ S3✓ ││
│  │ B11315008  蘇宗賢  四資工二乙  │  未評  │ ─── ─── ─── ││
│  │ B11315009  黃宥維  四資工二甲  │ 100/100│ S1✓ S2⏳ S3✓ ││  ← 自評 high-light
│  │ ...                                                      ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

**Grade 頁（數字 input 版）**：

```
┌─────────────────────────────────────────────────────────────┐
│  ← 返回   B11315009 黃宥維（四資工二甲） · 自評              │
│                                                              │
│  提示：Tab 切換欄位 · ↑/↓ 加減分 · Enter 送出                 │
│                                                              │
│  主題掌握    [  30  ] / 30                                   │
│  內容豐富度  [  30  ] / 30                                   │
│  敘述技巧    [  20  ] / 20                                   │
│  表現能力    [  10  ] / 10                                   │
│  團隊合作    [  10  ] / 10                                   │
│                                                              │
│  評語（選填，會同步到其他站）                                 │
│  ┌─────────────────────────────────────────────────────────┐│
│  │                                                          ││
│  └─────────────────────────────────────────────────────────┘│
│                                                              │
│  自備備註（選填，僅本地 + Site1）                             │
│  ┌─────────────────────────────────────────────────────────┐│
│  └─────────────────────────────────────────────────────────┘│
│                                                              │
│  總分 100 / 100                                               │
│  [ 送出並同步 ]                                               │
│                                                              │
│  ─── 歷史紀錄（點開展）───                                    │
│  2026-04-13 21:37  總分 100  [查看]                           │
│  2026-04-13 21:36  總分 95   [查看]                           │
└─────────────────────────────────────────────────────────────┘
```

**送出進度（送出後同頁展開，SSE 推更新）**：

```
┌─────────────────────────────────────────┐
│  送出中...                                │
│  Site1: ✓ 完成 (id=153, 0.8s)            │
│  Site2: ⏳ 進行中...                      │
│  Site3: ✓ 完成 (row=107, 1.2s)           │
└─────────────────────────────────────────┘
```

### 7.6 數字輸入欄位（score_input component）

**行為**：
- `<input type="number" min="0" max="{max}" step="1" value="{default}">`
- 預設填滿分（30/30/20/10/10）
- 原生 Tab 切換、↑/↓ 加減
- **JS 鎖滾輪**：focus 在欄位時 `wheel` 事件 `preventDefault()`
- JS clamp：`input` / `blur` 事件時強制 `value` 在 `[min, max]` 區間
- 超出範圍視覺提示（border 變琥珀）

**`static/js/grade.js`**：

```js
document.querySelectorAll('input[type=number][data-score]').forEach(input => {
    // 滾輪鎖：focus 時禁止 wheel 改值
    input.addEventListener('wheel', (e) => {
        if (document.activeElement === input) e.preventDefault();
    }, { passive: false });

    // clamp：超出範圍自動收回
    const clamp = () => {
        const min = Number(input.min);
        const max = Number(input.max);
        let v = Number(input.value);
        if (isNaN(v)) v = min;
        v = Math.max(min, Math.min(max, v));
        input.value = v;
        updateTotal();
    };
    input.addEventListener('input', clamp);
    input.addEventListener('blur', clamp);
});

function updateTotal() {
    const sum = [...document.querySelectorAll('input[type=number][data-score]')]
        .reduce((a, i) => a + (Number(i.value) || 0), 0);
    const totalEl = document.getElementById('total');
    totalEl.textContent = sum;
    totalEl.classList.toggle('text-warning', sum < 100);
}

// 首次進頁面自動 focus 第一欄
document.querySelector('input[type=number][data-score]')?.focus();
```

---

## 8. 工程細節

### 8.1 檔案結構

```
net-grading/
├── pyproject.toml
├── uv.lock
├── alembic.ini
├── tailwind.config.js
├── package.json
├── .env.example
├── .gitignore
├── main.py                           # uvicorn.run entrypoint
│
├── docs/
│   └── superpowers/
│       └── specs/
│           └── 2026-04-14-net-grading-sync-design.md   ← 本文件
│
├── src/net_grading/
│   ├── __init__.py
│   ├── app.py                        # FastAPI factory + lifespan
│   ├── config.py                     # pydantic-settings
│   ├── crypto.py                     # Fernet wrapper
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py
│   │   ├── models.py
│   │   └── migrations/
│   │       ├── env.py
│   │       └── versions/0001_init.py
│   │
│   ├── sites/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── errors.py
│   │   ├── site1.py
│   │   ├── site2.py
│   │   └── site3.py
│   │
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── middleware.py
│   │   └── session.py
│   │
│   ├── sync/
│   │   ├── __init__.py
│   │   ├── orchestrator.py
│   │   ├── pull.py
│   │   ├── conflict.py
│   │   ├── refresh_worker.py
│   │   └── sse.py
│   │
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── settings.py
│   │   ├── grading.py
│   │   ├── conflicts.py
│   │   └── sync.py
│   │
│   ├── templates/
│   │   ├── base.html
│   │   ├── _header.html
│   │   ├── login.html
│   │   ├── dashboard.html
│   │   ├── grade.html
│   │   ├── conflicts.html
│   │   ├── settings.html
│   │   ├── settings_site2.html
│   │   └── components/
│   │       ├── site_status_pill.html
│   │       ├── sync_progress.html
│   │       └── score_input.html
│   │
│   └── static/
│       ├── css/
│       │   ├── tokens.css
│       │   ├── input.css
│       │   └── app.css               # 編譯輸出 (gitignored)
│       ├── js/
│       │   ├── theme.js              # inline 到 <head>
│       │   ├── app.js
│       │   └── grade.js
│       └── favicon.svg
│
└── README.md                         # 留空（僅最小啟動指令）
```

### 8.2 Python 相依安裝（執行指令，不硬編碼版本）

```bash
uv add fastapi "uvicorn[standard]" jinja2 "sqlalchemy[asyncio]" aiosqlite alembic cryptography pydantic-settings sse-starlette python-multipart
# httpx[http2] 已在 pyproject.toml
```

### 8.3 Node 相依（僅 Tailwind CLI）

```json
{
  "devDependencies": {
    "tailwindcss": "^3.4",
    "@tailwindcss/forms": "^0.5"
  },
  "scripts": {
    "build:css": "tailwindcss -i src/net_grading/static/css/input.css -o src/net_grading/static/css/app.css --minify",
    "watch:css": "tailwindcss -i src/net_grading/static/css/input.css -o src/net_grading/static/css/app.css --watch"
  }
}
```

> 若偏好無 Node，改用 `standalone tailwindcss` 二進位檔，指令相同。

### 8.4 環境變數（`.env.example`）

```env
# App
APP_HOST=127.0.0.1
APP_PORT=8080
APP_ENV=production
LOG_LEVEL=INFO

# Security
SESSION_SECRET=               # python -c "import secrets; print(secrets.token_urlsafe(32))"
SITE2_ENC_KEY=                # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# External sites
SITE1_BASE_URL=https://api-ita.smashit.tw
SITE2_FIREBASE_API_KEY=AIzaSyCeFhJuKTm0UTOncIjNJ3YbUIsvspI-p-A
SITE2_FIREBASE_PROJECT=ntust-grading
SITE3_APPS_SCRIPT_URL=https://script.google.com/macros/s/AKfycbwQs_qm7GS-P3nshz4pgyjZ_XJslpl3BF_1t9UIKFYQRn49z8M_TIf36Qm0XRR79mi3Vw/exec

# DB
DATABASE_URL=sqlite+aiosqlite:///./net_grading.db
```

### 8.5 啟動指令

```bash
# 一次性
uv sync
npm install
cp .env.example .env && vim .env
uv run alembic upgrade head

# 開發
npm run watch:css &
uv run uvicorn net_grading.app:app --reload --host 127.0.0.1 --port 8080

# 正式
npm run build:css
uv run uvicorn net_grading.app:app --host 127.0.0.1 --port 8080
```

### 8.6 Nginx / Cloudflare 注意事項

- **SSE**：`/sync/*/events` 需關 nginx buffering
  ```nginx
  location ~ ^/sync/.+/events$ {
      proxy_buffering off;
      proxy_read_timeout 3600s;
      proxy_pass http://127.0.0.1:8080;
  }
  ```
- **Cloudflare**：`/sync/*/events` 設 Cache Rule bypass（否則 CF 會 buffer 住 SSE）
- **Cookie `Secure`**：透過 Cloudflare 必然 HTTPS，OK
- **`X-Forwarded-For`**：若要記錄 client IP 到 `sync_logs`，FastAPI 需配 `ProxyHeadersMiddleware`

---

## 9. 開放問題 / 實作時需確認

1. **Site1 `need_password=true` 的密碼端點**
   Endpoints-PoC 只展示 `need_password=false` 的 identify 請求。實作時需要：
   - 向 Site1 作者（SamWang8891）確認密碼登入 API 路徑與請求格式
   - 或直接看 Site1 源碼 https://github.com/SamWang8891/ita-grading 找 `auth/password` 路由

2. **Site2 Firestore `grades` 集合的 schema**
   PoC 的 Bearer token 已過期（1h 前發）。實作 `Site2Client.list_submissions` 時需要：
   - 使用者提供新的測試 Firebase 帳號密碼
   - 打 Firestore `runQuery` 驗證 `grades` 集合實際欄位（對應到 `score_topic / content / ...` 怎麼命名）
   - 驗證「同一 target 多次送出」是怎麼儲存（是覆蓋還是 append）

3. **Site2 送出的 Firestore document 結構**
   - `documents/grades/{doc_id}` 寫入時 `doc_id` 規則是什麼？`graderId_targetId_period`？時間戳？
   - 要用 `patch` 還是 `createDocument`？

4. **Site3 Apps Script 錯誤格式**
   - PoC 只展示 `{"result":"success"}`；失敗時回什麼？需實測

5. **`self_note` 在 Site2/3 有沒有欄位？**
   - Site1 有；Site2/3 似乎沒有 → 本文設計假設「僅同步到 Site1」。若 Site2 schema 有此欄位，實作時調整。

6. **Site1 送出是否支援 PATCH / 覆蓋舊筆**
   - PoC 每次 POST `/api/student/submissions` 都產生新 id（append）→ 我們也採 append 策略，OK

---

## 10. 下一步

1. 本文件完成 → git commit
2. **使用者 review 書面版**，有任何修改點告知
3. 通過後 → 進 `writing-plans` skill，產出逐階段實作計畫（含：
   - 分 milestone 的實作順序
   - 每個 milestone 的 DoD（可啟動/可登入/可送出單站/可三站同步/可解衝突…）
   - 每個 commit 的範圍
   - 實作時的「第一道煙霧測試」指令
   ）
