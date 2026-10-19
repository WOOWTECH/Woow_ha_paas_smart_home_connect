# woow-paas-smart-home

> 上游是 Gitea `ha-components/woow_paas_smart_home`；GitHub `WOOWTECH/Woow_ha_paas_smart_home_connect` 是唯讀鏡像，由 `.gitea/workflows/mirror-to-github.yml`（main／tag 有變動時＋每 8 小時補推）用 GitHub 部署金鑰推過去，兩邊同一個 commit。PR 請開在 Gitea；鏡像只接受 fast-forward，不會強制覆寫 GitHub；經核准直接在 GitHub 發布時，必須同步回 Gitea，避免後續鏡像因分支差異失敗。

這是 homeassitant custom component 會和 woow-paas-platform 整合

使用 Config entries 設定一個 woow paas smart home
1. 和 woow-paas-platform 做 Oauth 串接，登入 woow-paas-platform
2. 登入後，使用者會先選一個 workspace 再選一個 smart home ，然後就會回傳 Cloudflare Tunnel 讓 HA custom component Token 自動部署 cloudflared 進程。

## Home Assistant 反向代理設定

cloudflared 以本機 Home Assistant 為來源時，必須明確允許本機反向代理；`localhost` 可能使用 IPv4 或 IPv6。

### HA 2026.8 以上

到 **設定 → 系統 → 網路 → HTTP 伺服器**，開啟信任 `X-Forwarded-For`，在信任代理填入 `127.0.0.1/32` 與 `::1/128`。保留目前連接埠（新版 HAOS 預設為 80），儲存後 HA 會重啟。確認內外網均可連線後，在五分鐘內確認新設定，否則 HA 會自動回復。

此版本已有 HTTP 設定儲存時，新增 YAML 區段不會覆蓋目前設定；請使用介面。詳見 [Home Assistant 官方 HTTP 說明](https://www.home-assistant.io/integrations/http/)。

### 較舊的 HA

在 `configuration.yaml` 合併下列設定：

```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 127.0.0.1/32
    - ::1/128
```

合併到既有 `http:` 區段後依 HA 提示重啟。來源使用其他容器／位址時，應改成
實際代理的精確位址；不要為此信任整個 LAN。Tunnel 顯示 connected 僅表示通道
已連線，仍需從對外入口確認 HA 登入頁與登入後的 REST／WebSocket 正常。

## cloudflared 程序恢復

成功啟動的受管程序若意外退出，整合會在30秒後使用同一份 tunnel token 重啟。
重啟失敗會倍增等待，最多600秒；成功後回到30秒。手動停止、停用／卸載整合
及 HA shutdown 會取消這個恢復意圖，因此不會因 watcher 而自動復活。
首次 binary 安裝／啟動重試仍由既有 setup 流程處理。受管程序退出或成功啟動時，
本機狀態會立即通知 sensor；PaaS 遠端狀態仍使用既有輪詢，不增加請求或縮短重試。
實際可用性應另驗證對外登入與請求。

獨立程序生命週期回歸可執行 `python scripts/test_watchdog.py`（需pytest及aiohttp）；HA wiring 使用
stub，child／PID／signal／lock／取消則是真OS操作。完整原生 HA 故障驗收仍需
在隔離實例進行。

## Smart Home 自動撤權（access_revision）

平台在 Cloudflare Access 收緊某條受保護 route（例如移除成員）後，會把 home
狀態的 `access_revision` 往前推。HA 只在**成功**的 Smart Home 狀態輪詢讀這個
欄位：第一次看到當作基準；之後數值改變（含變小）就等 5 秒讓 Cloudflare 傳播，
再於背景呼叫 `reset_tunnel()` 斷線重連，讓既有連線全部重新經過 Access。
**同一個家庭所有遠端連線會短暫中斷**。執行中又有新版本會合併成一次、完成後若仍
落後再跑一次；未確認舊 connector 退出或發生例外則不前進，之後的輪詢以 30 秒起、
最多 300 秒的退避重試。手動停止隧道時沒有連線可斷，直接記為已套用。

tunnel status sensor 會帶 `access_revision`、`access_revision_applied` 與
`last_access_reset`（時間、`cutoff_verified`、`restarted`）；不含任何 token。
舊平台不帶欄位或型別不對時完全不動作，attributes 維持原樣。

限制：平台連不上時 HA 看不到新版本（沒有 lease）；撤權結果不回報平台；
Security Access 尚未接上；斷線後若新 connector 起不來，需手動 `start_tunnel`
或重載整合。`cutoff_verified` 只代表本機舊 connector 已退出，不代表 Cloudflare
端全域撤銷。

## Security Access routes

選擇 Security Access 產品時，每一條 application route 會對應一個獨立的 URL sensor，並隨 PaaS 端的 route 異動在 30 秒輪詢內自動增減（免 reload）。

⚠️ **修改 route 的 hostname（subdomain prefix）等同重建**：PaaS 端的 hostname 是不可變的識別錨點，改名在後端是「刪除舊 route + 建立新 route」（產生新的 route id）。因此在 PaaS 改 route hostname 後，HA 端原本的 sensor 會變成 unavailable、並出現一個全新的 sensor，**該 route 的歷史資料與參照舊 entity_id 的 automation／dashboard 不會延續**。若只是調整 `service_url`、`is_protected` 等其他欄位則 route id 不變，sensor 會原地更新、保留歷史。
## MCP 自動安裝與連線網址

選擇「遠端存取＋MCP」後，本整合會在確認平台訂閱含 MCP 時：

1. 檢查 Home Assistant 版本（嵌入式 MCP 需 2026.6.0 以上）。
2. 尚未安裝時，下載 `WOOWTECH/Woow_ha_ai_mcp` 的固定 commit
   `c7b8d79fea7228007d7af9291f3a03afc4d3e071`，校驗 SHA-256，再安裝其
   `custom_components/ha_mcp_tools`（1.2.2）。現有套件檔案不會被覆蓋。
3. 透過 HA-MCP 原生流程建立唯一的 **HA-MCP Server**，由它安裝 server
   依賴、產生密鑰並啟動 webhook。首次安裝使用與 HACS 相同的快取更新方式；
   不會自動重新啟動整台 HA。安裝與遠端隧道各自運作。
4. 已有 server 時直接沿用；僅有 tools entry 時仍會建立 server。
   手動停用 server、關閉 webhook 或自訂 OAuth 的選擇會保留。
5. 在診斷區顯示「MCP 工具」及 **MCP URL**。安裝中、啟動中分開顯示；
   確認伺服器運作且 webhook 註冊完成後才提供完整 URL。

MCP server 使用 HA-MCP 的原生穩定版安裝及更新設定。固定 commit 校驗的是
HA component，不表示將 PyPI 的 `ha-mcp` server 或其依賴也鎖在相同版本。
單選遠端存取、Security Access 與未含 MCP 的訂閱不會觸發 MCP 安裝。
退訂會移除本整合的 MCP sensors，但不刪除可能被其他用途使用的 HA-MCP server。

網址格式為 `https://<此 Smart Home 的隧道主機>/api/webhook/<隨機密鑰>`。
HTTPS 加密的是傳輸；網址路徑本身是存取密鑰，並非將 URL 字串加密。
若原有 server 啟用 HA OAuth，client 仍需完成該驗證。
MCP URL 恢復放在 sensor 的 state；既有 unique_id、entity_id 及裝置關係保留。
點開 sensor 詳細視窗即可選取複製；不另外在 attributes 重複存放 webhook 密鑰。
沒有可用端點時顯示 unknown，不會保留上一個密鑰。

此 sensor 可被有 HA 狀態讀取權限的帳號、模板與 recorder 讀取。
若不希望保存 URL 歷史，可在 `recorder.exclude.entities` 加入實際的 sensor entity_id；
既有名稱可能仍為 `sensor.<裝置>_mcp_connection_availability`，因此請以實體設定為準。
本整合不會修改全站 recorder 設定或清除歷史，也不會輪換既有密鑰。

### Portable watchdog regression gate

Pull requests run `watchdog-regression` with Python 3.13. Reproduce its test
steps in a disposable environment:

```bash
python3.13 -m venv .venv-watchdog
.venv-watchdog/bin/python -m pip install -r scripts/requirements-watchdog.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv-watchdog/bin/python -B scripts/test_watchdog.py
```

Use this runner instead of collecting `watchdog_tests` directly from the
integration root: direct collection imports the entire HA integration. The
runner copies the unchanged tests to a temporary directory and loads this
checkout's `cloudflared_manager.py` and `access_revocation.py`. It exercises real processes, signals,
locks and cancellation with synthetic credentials and stubbed HA wiring.
It does not establish native HA integration, live Cloudflare, or production
acceptance; those remain separate tests.
