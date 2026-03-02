---
name: woow-paas-smart-home-init
description: Initialize woow_paas_smart_home HA custom component with OAuth2 login, workspace/home selection, Cloudflare Tunnel auto-deployment, and tunnel status sensors
status: backlog
created: 2026-03-02T09:45:56Z
updated: 2026-03-02T14:29:03Z
---

# PRD: woow-paas-smart-home-init

## Executive Summary

建立 `woow_paas_smart_home` Home Assistant custom component，整合 woow-paas-platform。使用者透過 OAuth2 Authorization Code Flow 登入平台，選擇 workspace 和 smart home 後，component 自動取得 Cloudflare Tunnel token 並部署 `cloudflared` 進程，讓 Home Assistant 實例可透過 Cloudflare Tunnel 對外提供服務。同時提供 sensor entity 即時顯示 tunnel 連線狀態與 subdomain 資訊。

## Problem Statement

目前 Home Assistant 使用者若要透過 woow-paas-platform 管理 smart home，需要手動設定 tunnel 連線。這個流程複雜且容易出錯。我們需要一個 custom component，讓使用者透過簡單的 UI 完成 OAuth 登入、選擇 smart home、並自動部署 Cloudflare Tunnel，並能在 HA dashboard 上直接監控 tunnel 狀態。

## User Stories

### US-1: 使用者首次設定 component
**As a** Home Assistant 使用者
**I want to** 在 HA UI 上透過 OAuth2 登入 woow-paas-platform 並選擇我的 smart home
**So that** Cloudflare Tunnel 自動建立，我的 HA 可透過 tunnel 對外服務

**Acceptance Criteria:**
- Config flow 第一步引導使用者前往 woow-paas-platform OAuth2 授權頁面
- 授權完成後自動回到 HA，取得 access token 和 refresh token
- 第二步列出使用者可存取的 workspaces 供選擇
- 第三步列出該 workspace 下的 smart homes 供選擇
- 選擇完成後自動取得 tunnel token 並啟動 cloudflared

### US-2: 使用者管理 tunnel
**As a** Home Assistant 使用者
**I want to** 透過 HA service 控制 tunnel 啟停並查看狀態
**So that** 我可以在需要時啟動或停止 tunnel

**Acceptance Criteria:**
- 提供 `start_tunnel` service 啟動 cloudflared tunnel
- 提供 `stop_tunnel` service 停止 cloudflared tunnel
- 提供 `get_status` service 查詢 tunnel 和 smart home 狀態

### US-3: HA 重啟後自動恢復 tunnel
**As a** Home Assistant 使用者
**I want to** HA 重啟後 tunnel 自動恢復連線
**So that** 不需要手動重新啟動 tunnel

**Acceptance Criteria:**
- Component 在 `async_setup_entry` 時自動在背景啟動 cloudflared
- 若 token 過期，自動使用 refresh token 刷新
- 啟動失敗時記錄錯誤日誌但不阻塞 HA 啟動

### US-4: 使用者在 Dashboard 監控 tunnel 狀態
**As a** Home Assistant 使用者
**I want to** 在 HA Dashboard 上看到 tunnel 的即時連線狀態和 subdomain 網址
**So that** 我可以快速確認 tunnel 是否正常運作

**Acceptance Criteria:**
- `binary_sensor.{name}_tunnel_connected` 顯示 tunnel 是否連線（on/off）
- `sensor.{name}_tunnel_status` 顯示詳細狀態文字（connected/disconnected/error/starting）
- `sensor.{name}_tunnel_url` 顯示 tunnel 的 subdomain 完整 URL
- 狀態每 30 秒自動更新（本地進程檢查 + 遠端 API 查詢）

## Requirements

### Functional Requirements

#### FR-1: OAuth2 Authorization Code Flow + PKCE
- 實作 HA 標準的 OAuth2 config flow（使用 `homeassistant.helpers.config_entry_oauth2_flow`）
- OAuth2 endpoints:
  - Authorization: `{BASE_URL}/oauth2/authorize`
  - Token: `{BASE_URL}/oauth2/token`
- 請求 scopes: `smarthome:read smarthome:tunnel workspace:read`
- 支援 PKCE（code_challenge_method=S256）
- 儲存 access_token 和 refresh_token，支援自動刷新

#### FR-2: Multi-step Config Flow
- **Step 1 - OAuth Login**: 引導使用者完成 OAuth2 授權
- **Step 2 - Select Workspace**: 呼叫 `GET /api/smarthome/workspaces` 列出 workspaces
- **Step 3 - Select Smart Home**: 呼叫 `GET /api/smarthome/workspaces/{id}/homes` 列出 homes
- 完成後呼叫 `GET /api/smarthome/homes/{id}/tunnel-token` 取得 tunnel 憑證
- 將所有資料儲存到 config entry data

#### FR-3: Cloudflare Tunnel 自動部署
- 自動偵測 OS/架構，下載對應的 `cloudflared` binary
- 使用 tunnel token 啟動 `cloudflared` 進程
- 管理 cloudflared 進程的生命週期（啟動、停止、狀態監控）
- 支援平台：Linux (x86_64, aarch64), macOS (amd64, arm64)

#### FR-4: Services
- `woow_paas_smart_home.start_tunnel` - 啟動指定 smart home 的 tunnel
- `woow_paas_smart_home.stop_tunnel` - 停止指定 smart home 的 tunnel
- `woow_paas_smart_home.get_status` - 查詢 tunnel 狀態（回傳 ServiceResponse）

#### FR-5: API Client
- 封裝所有 woow-paas-platform API 呼叫
- 注入 `OAuth2Session` 自動帶 Bearer Token 並自動刷新
- API endpoints:
  - `GET /api/smarthome/workspaces` (scope: workspace:read)
  - `GET /api/smarthome/workspaces/{id}/homes` (scope: smarthome:read)
  - `GET /api/smarthome/homes/{id}` (scope: smarthome:read)
  - `GET /api/smarthome/homes/{id}/tunnel-token` (scope: smarthome:tunnel)
  - `GET /api/smarthome/homes/{id}/status` (scope: smarthome:read)

#### FR-6: Tunnel Status Entities
- **`binary_sensor.{name}_tunnel_connected`** — tunnel 是否連線（on = 連線中, off = 斷線）
  - 資料來源：本地 cloudflared 進程是否存活 AND 遠端 API tunnel_status == "connected"
- **`sensor.{name}_tunnel_status`** — tunnel 詳細狀態
  - 可能值：`connected`, `disconnected`, `error`, `starting`, `unknown`
  - 資料來源：合併本地進程狀態 + 遠端 API `tunnel_status` 欄位
- **`sensor.{name}_tunnel_url`** — tunnel 的 subdomain 完整 URL
  - 格式：`https://{subdomain}` 或空字串（未連線時）
  - 資料來源：config entry data 中的 subdomain 或遠端 API

#### FR-7: DataUpdateCoordinator
- 使用 `DataUpdateCoordinator` 統一管理狀態更新
- 更新間隔：30 秒
- 每次更新同時檢查：
  1. 本地 cloudflared 進程是否存活（`process.poll()`）
  2. 遠端 API `/homes/{id}/status` 取得平台端狀態
- 合併兩個來源產生最終狀態供 entity 使用

### Non-Functional Requirements

#### NFR-1: 效能
- cloudflared 進程啟動不應阻塞 HA 啟動流程
- API 呼叫使用 aiohttp async 操作
- cloudflared binary 下載使用非同步串流
- 本地進程檢查應為 non-blocking（process.poll()）

#### NFR-2: 安全性
- OAuth2 token 安全儲存在 config entry data 中
- 支援 PKCE 防止 authorization code 劫持
- tunnel token 不記錄到日誌中
- cloudflared binary 下載後驗證完整性

#### NFR-3: 可靠性
- cloudflared 進程異常退出時自動重啟
- Token 過期自動刷新（透過 HA OAuth2Session）
- 網路中斷時優雅降級（sensor 顯示 error/unknown 而非崩潰）
- Coordinator 更新失敗時使用上次成功的資料

## Success Criteria

- Config flow 可完整走完 OAuth → workspace 選擇 → smart home 選擇
- cloudflared 可成功啟動並建立 tunnel 連線
- HA 重啟後 tunnel 自動恢復
- Services（start/stop/status）可正常運作
- Sensor entities 正確反映 tunnel 即時狀態
- binary_sensor 在 tunnel 斷線時正確顯示 off

## Constraints & Assumptions

### Constraints
- woow-paas-platform API base URL 使用占位符，實際部署時配置
- cloudflared binary 需要寫入權限（儲存在 HA config 目錄下）
- 需要網路存取才能下載 cloudflared 和連接 API

### Assumptions
- woow-paas-platform OAuth2 server 已配置好 client_id 和 redirect_uri
- Smart home 在 platform 端已完成 provision（有 tunnel_token）
- 目標環境為 Linux ARM64（樹莓派）或 x86_64，以及 macOS 開發環境

## Out of Scope

- HA Diagnostics 整合（未來版本）
- 多個 smart home 同時管理（每個 config entry 對應一個 smart home）
- cloudflared 的自動更新機制
- woow-paas-platform 的使用者註冊流程

## Dependencies

### External Dependencies
- woow-paas-platform OAuth2 server
- woow-paas-platform REST API (`/api/smarthome/*`)
- Cloudflare Tunnel 服務
- cloudflared binary 下載源

### Internal Dependencies
- Home Assistant Core >= 2024.1
- Python >= 3.12
- aiohttp（HA 內建）

## Technical Architecture

### File Structure
```
woow_paas_smart_home/
├── __init__.py                 # async_setup (services) + async_setup_entry (組裝各元件)
├── config_flow.py              # AbstractOAuth2FlowHandler + workspace/home 選擇步驟
├── application_credentials.py  # HA OAuth2 client_id/secret 配置
├── const.py                    # Constants (domain, API URLs, config keys, platforms)
├── coordinator.py              # DataUpdateCoordinator (本地+遠端狀態合併)
├── api_client.py               # woow-paas-platform API client (注入 OAuth2Session)
├── cloudflared_manager.py      # cloudflared binary 下載 + 進程管理
├── sensor.py                   # tunnel_status + tunnel_url sensor entities
├── binary_sensor.py            # tunnel_connected binary_sensor entity
├── manifest.json               # Integration manifest
├── strings.json                # UI text and translations
└── services.yaml               # Service definitions
```

### Key Design Decisions
1. **OAuth2**: 使用 HA 內建 `AbstractOAuth2FlowHandler` + `OAuth2Session`，自動處理 token refresh，不手動管理
2. **API Client**: 注入 `OAuth2Session`，所有 API 呼叫自動帶 Bearer Token
3. **CloudflaredManager**: 獨立類別，只負責 binary 下載和進程管理，不依賴 API client
4. **DataUpdateCoordinator**: 統一協調本地進程檢查 + 遠端 API 查詢，供所有 entity 使用
5. **扁平結構**: 不使用 `libs/` 子目錄，符合 HA 社區慣例
6. **不使用 Hub Pattern**: 改用 Coordinator + 獨立元件組裝，避免 God Object

### Data Flow
```
DataUpdateCoordinator (每 30 秒)
  ├── CloudflaredManager.is_running() → 本地進程狀態
  └── ApiClient.get_home_status(id) → 遠端 tunnel_status
  │
  ▼ 合併為 CoordinatorData
  ├── binary_sensor (connected: bool)
  ├── sensor tunnel_status (status: str)
  └── sensor tunnel_url (url: str)
```
