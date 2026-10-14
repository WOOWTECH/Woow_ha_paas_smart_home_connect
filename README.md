# woow-paas-smart-home

> 上游是 Gitea `ha-components/woow_paas_smart_home`；GitHub `WOOWTECH/Woow_ha_paas_smart_home_connect` 是唯讀鏡像，由 `.gitea/workflows/mirror-to-github.yml`（main／tag 有變動時＋每 8 小時補推）用 GitHub 部署金鑰推過去，兩邊同一個 commit。PR 請開在 Gitea；直接改 GitHub 的內容會在下次同步時被覆蓋。

這是 homeassitant custom component 會和 woow-paas-platform 整合

使用 Config entries 設定一個 woow paas smart home
1. 和 woow-paas-platform 做 Oauth 串接，登入 woow-paas-platform
2. 登入後，使用者會先選一個 workspace 再選一個 smart home ，然後就會回傳 Cloudflare Tunnel 讓 HA custom component Token 自動部署 cloudflared 進程。

## Home Assistant 反向代理設定

cloudflared 以本機 Home Assistant 為來源時，請先在 HA 的 `configuration.yaml`
明確允許本機反向代理；`localhost` 可能使用 IPv4 或 IPv6：

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

## Security Access routes

選擇 Security Access 產品時，每一條 application route 會對應一個獨立的 URL sensor，並隨 PaaS 端的 route 異動在 30 秒輪詢內自動增減（免 reload）。

⚠️ **修改 route 的 hostname（subdomain prefix）等同重建**：PaaS 端的 hostname 是不可變的識別錨點，改名在後端是「刪除舊 route + 建立新 route」（產生新的 route id）。因此在 PaaS 改 route hostname 後，HA 端原本的 sensor 會變成 unavailable、並出現一個全新的 sensor，**該 route 的歷史資料與參照舊 entity_id 的 automation／dashboard 不會延續**。若只是調整 `service_url`、`is_protected` 等其他欄位則 route id 不變，sensor 會原地更新、保留歷史。
## MCP 連線可用性與私密網址

訂閱含 MCP 的方案時，`MCP Tools` 顯示本機整合的執行狀態，原 `MCP URL`
實體改為顯示連線可用性：`available`、`unavailable`，尚無資料時為 `unknown`。
`available` 表示已有 tunnel URL 與已載入 MCP 整合的 webhook 識別資料；
它不代替對外 HTTP、身分驗證或工具權限的實際檢查。

完整 MCP 網址包含 webhook 憑證，不再放進 HA sensor 的 state 或屬性。
HA 管理員請到「設定 → 裝置與服務 → Home Assistant MCP → HA-MCP Server
→ 設定」的原生 MCP 選項流程取得 **Connect URL(s)／Remote connect URL**，
再交給已授權的 client。若網址不是本次 Smart Home Connect 的對外主機，
在 MCP 的 External URL 選項填入該訂閱的 tunnel URL，儲存後重新開啟設定
確認網址；不要將 webhook 憑證放回一般 sensor 或 dashboard。
一般使用者可讀的 dashboard、模板與歷史頁面只能取得可用性狀態。

既有實體的 unique_id 保留，因此已登錄的 entity_id 與裝置關係保留。
原本把該 sensor 當作網址使用的 automation、template 或 client 設定需改用
管理員提供的連線資料；這次更新改變的是 state 契約，不提供原網址的兼容欄位。

更新只防止新的 state／attribute 憑證發布。舊 recorder、logbook、備份或
已複製的網址不會因此清除或失效。正式更新前應由管理員盤點使用者，安排
該 MCP endpoint 的憑證輪換，再依保存政策處理該實體的歷史與備份。
本整合不會自動 purge 歷史、變更 HA-MCP 身分驗證或關閉其寫入工具。
伺服器的 HA 管理員驗證、唯讀限制、套件隔離與外部 client OAuth 相容性
仍須各自驗證。

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
checkout's `cloudflared_manager.py`. It exercises real processes, signals,
locks and cancellation with synthetic credentials and stubbed HA wiring.
It does not establish native HA integration, live Cloudflare, or production
acceptance; those remain separate tests.
