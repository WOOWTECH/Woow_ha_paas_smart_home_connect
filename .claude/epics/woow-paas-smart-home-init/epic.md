---
name: woow-paas-smart-home-init
status: completed
created: 2026-03-02T14:34:05Z
updated: 2026-03-03T00:51:15Z
completed: 2026-03-03T00:51:15Z
progress: 100%
prd: .claude/prds/woow-paas-smart-home-init.md
github: N/A (GitLab repo, local task management only)
---

# Epic: woow-paas-smart-home-init

## Overview

初始化 `woow_paas_smart_home` Home Assistant custom component。實作 OAuth2 登入 woow-paas-platform、多步驟 config flow（workspace → smart home 選擇）、自動下載並管理 cloudflared 進程建立 Cloudflare Tunnel，以及提供 sensor/binary_sensor entity 顯示 tunnel 即時狀態。

## Architecture Decisions

| 決策 | 選擇 | 理由 |
|------|------|------|
| Auth 方式 | HA 內建 `AbstractOAuth2FlowHandler` + `OAuth2Session` | 自動處理 token refresh、PKCE，不需手動管理 token 生命週期 |
| 狀態管理 | `DataUpdateCoordinator` | 統一管理本地進程 + 遠端 API 狀態輪詢，天然支援 entity 資料更新 |
| Tunnel 管理 | 獨立 `CloudflaredManager` class | 職責單一，只接收 tunnel_token，不依賴 API client，可獨立測試 |
| API 封裝 | `ApiClient` 注入 `OAuth2Session` | 所有 API 呼叫自動帶 Bearer Token + 自動 refresh |
| 檔案結構 | 扁平（無 `libs/` 子目錄） | 符合 HA 社區慣例，減少不必要的層級 |
| 不使用 Hub Pattern | Coordinator + 獨立元件在 `__init__.py` 組裝 | 避免 God Object，各元件職責明確 |

## Technical Approach

### Component 核心檔案

```
woow_paas_smart_home/
├── __init__.py                 # async_setup (services) + async_setup_entry (組裝)
├── const.py                    # DOMAIN, API URLs, config keys, platforms list
├── manifest.json               # Integration metadata
├── strings.json                # UI 文字
├── services.yaml               # Service 定義
├── application_credentials.py  # HA OAuth2 client credentials 配置
├── config_flow.py              # OAuth2 flow + workspace/home 選擇步驟
├── api_client.py               # woow-paas-platform REST API client
├── cloudflared_manager.py      # cloudflared binary 下載 + 進程管理
├── coordinator.py              # DataUpdateCoordinator (每 30 秒合併狀態)
├── sensor.py                   # tunnel_status + tunnel_url sensors
└── binary_sensor.py            # tunnel_connected binary_sensor
```

### Data Flow

```
async_setup_entry
  ├── OAuth2Session(hass, entry)           → 自動 token 管理
  ├── ApiClient(session)                   → API 存取
  ├── CloudflaredManager(hass)             → tunnel 進程管理
  └── TunnelCoordinator(hass, api, mgr)    → 狀態輪詢
        │
        ├─ 每 30 秒 ──┬── mgr.is_running()          → 本地進程狀態
        │              └── api.get_home_status(id)    → 遠端 tunnel_status
        │
        ▼ CoordinatorData
        ├── binary_sensor (connected: bool)
        ├── sensor tunnel_status (str)
        └── sensor tunnel_url (str)
```

### Config Flow 步驟

```
Step 1: OAuth2 Authorization Code Flow + PKCE
  → 使用者在 woow-paas-platform 授權
  → 回到 HA 取得 access_token / refresh_token

Step 2: Select Workspace
  → GET /api/smarthome/workspaces → 列出下拉選單

Step 3: Select Smart Home
  → GET /api/smarthome/workspaces/{id}/homes → 列出下拉選單

Final: 取得 tunnel token
  → GET /api/smarthome/homes/{id}/tunnel-token
  → 儲存 tunnel_token, tunnel_id, subdomain 到 config entry
  → 建立 entry
```

## Implementation Strategy

採**由底到頂**的開發順序：先建立基礎設施（const, manifest, API client），再建構 config flow，然後 tunnel 管理，最後加上 coordinator 和 entity。

### 開發階段

1. **Phase 1 — 骨架 + API**: const, manifest, strings, application_credentials, api_client
2. **Phase 2 — Config Flow**: OAuth2 flow + workspace/home selection steps
3. **Phase 3 — Tunnel 管理**: cloudflared_manager + __init__.py entry setup + services
4. **Phase 4 — 狀態監控**: coordinator + sensor + binary_sensor entities

### 測試策略
- 各模組可獨立單元測試（api_client mock HTTP、cloudflared_manager mock subprocess）
- Config flow 使用 HA test utilities
- Integration test 需要實際 woow-paas-platform 環境

## Task Breakdown Preview

- [ ] Task 1: 建立 component 骨架（const.py, manifest.json, strings.json, services.yaml）
- [ ] Task 2: 實作 application_credentials.py + OAuth2 基礎設定
- [ ] Task 3: 實作 api_client.py（封裝所有 woow-paas-platform API）
- [ ] Task 4: 實作 config_flow.py（OAuth2 + workspace/home 多步驟選擇）
- [ ] Task 5: 實作 cloudflared_manager.py（binary 下載 + 進程管理）
- [ ] Task 6: 實作 __init__.py（entry setup/unload + services 註冊）
- [ ] Task 7: 實作 coordinator.py + sensor.py + binary_sensor.py（狀態監控 entities）

## Dependencies

### External
- woow-paas-platform OAuth2 server（需有已註冊的 client_id + redirect_uri）
- woow-paas-platform REST API（`/api/smarthome/*` endpoints）
- Cloudflare Tunnel 服務
- cloudflared binary 官方下載源（github.com/cloudflare/cloudflared/releases）

### Prerequisites
- woow-paas-platform 上至少有一個已 provision 的 smart home（有 tunnel_token）
- HA 開發環境已設定好（`hass -c config`）

## Success Criteria (Technical)

| 指標 | 目標 |
|------|------|
| Config flow | 完整走完 OAuth → workspace → home → 建立 entry |
| Tunnel 啟動 | cloudflared 進程成功啟動並建立 tunnel |
| HA 重啟 | 自動恢復 tunnel 連線 |
| Token refresh | OAuth2 token 過期後自動刷新，不中斷服務 |
| Sensor 狀態 | binary_sensor 正確反映 on/off，sensor 顯示正確狀態文字 |
| Services | start_tunnel/stop_tunnel/get_status 可正常呼叫 |
| 錯誤處理 | API 失敗或進程崩潰時 sensor 顯示 error/unknown，不造成 HA 崩潰 |

## Estimated Effort

- **Task 1** (骨架): 小 — 靜態檔案建立
- **Task 2** (OAuth2 credentials): 小 — HA 標準 pattern
- **Task 3** (API client): 中 — 5 個 endpoint 封裝 + 錯誤處理
- **Task 4** (Config flow): 大 — OAuth2 + 多步驟 UI，最複雜的部分
- **Task 5** (Cloudflared manager): 大 — binary 下載 + 進程管理 + 跨平台
- **Task 6** (__init__.py): 中 — 組裝各元件 + services
- **Task 7** (Coordinator + entities): 中 — 標準 HA coordinator pattern

**總計 7 個 tasks**，建議按順序開發，Task 4 和 Task 5 可平行進行。

## Tasks Created
- [ ] 001.md - Component 骨架與常數定義 (parallel: true)
- [ ] 002.md - OAuth2 Application Credentials 設定 (parallel: true)
- [ ] 003.md - API Client 實作 (parallel: true)
- [ ] 004.md - Config Flow 實作（OAuth2 + 多步驟選擇）(parallel: false, depends: 002, 003)
- [ ] 005.md - Cloudflared Manager 實作 (parallel: true)
- [ ] 006.md - Component Entry Setup 與 Services 註冊 (parallel: false, depends: 003, 004, 005)
- [ ] 007.md - DataUpdateCoordinator 與 Sensor Entities (parallel: false, depends: 006)

Total tasks: 7
Parallel tasks: 4 (001, 002, 003, 005)
Sequential tasks: 3 (004, 006, 007)

### Dependency Graph
```
001 ─────────────────────────────────┐
002 ──┐                              │
003 ──┼──→ 004 ──┐                   │
005 ─────────────┼──→ 006 ──→ 007   │
                 │                   │
                 └───────────────────┘
```
