# woow-paas-smart-home

這是 homeassitant custom component 會和 woow-paas-platform 整合

使用 Config entries 設定一個 woow paas smart home
1. 和 woow-paas-platform 做 Oauth 串接，登入 woow-paas-platform
2. 登入後，使用者會先選一個 workspace 再選一個 smart home ，然後就會回傳 Cloudflare Tunnel 讓 HA custom component Token 自動部署 cloudflared 進程。

## Security Access routes

選擇 Security Access 產品時，每一條 application route 會對應一個獨立的 URL sensor，並隨 PaaS 端的 route 異動在 30 秒輪詢內自動增減（免 reload）。

⚠️ **修改 route 的 hostname（subdomain prefix）等同重建**：PaaS 端的 hostname 是不可變的識別錨點，改名在後端是「刪除舊 route + 建立新 route」（產生新的 route id）。因此在 PaaS 改 route hostname 後，HA 端原本的 sensor 會變成 unavailable、並出現一個全新的 sensor，**該 route 的歷史資料與參照舊 entity_id 的 automation／dashboard 不會延續**。若只是調整 `service_url`、`is_protected` 等其他欄位則 route id 不變，sensor 會原地更新、保留歷史。
## MCP URL

訂閱含 MCP 的方案（`/status` 的 `mcp == "ha_mcp_tools"`）時，除了 `MCP Tools` 狀態
sensor 之外，還會有一顆 `MCP URL`，**state 就是 MCP client 要填的完整連線位址**：

```
https://{subdomain}.woowtech.io/api/webhook/mcp_{32 hex}
```

由這張訂閱的 tunnel 網址，接上本機 `ha_mcp_tools` 生效中的 webhook id 組成。
另帶兩個屬性 `webhook_id`、`base_url`（組成部分，方便自己排版）。

三個條件都成立才有值，否則為 `unknown`：`ha_mcp_tools` 的 config entry 已載入、
它的 `enable_webhook` 選項不是 `False`（local-only 模式下端點不存在）、tunnel 已連上。

```yaml
# 模板／自動化
{{ states('sensor.<裝置>_mcp_url') }}
```

⚠️ **這個值是憑證。** `ha_mcp_tools` 預設 `webhook_auth=none`，網址本身就是進入 MCP
server 的鑰匙。它會進 recorder 歷史，也會出現在任何顯示它的儀表板。

### 桌面版裝置頁那一列會擠壓（已知取捨）

**點開實體看詳細視窗是正常的**——主狀態區是 `word-break: break-word`，長網址會正常
換行、可直接選取複製，手機版尤其清楚。有問題的只有桌面版**裝置頁診斷卡那一列**：
網址會溢出、左邊的名稱被擠到只剩一個字。

這是 Home Assistant 前端的版面限制，不是本整合能修的。量測如下：

| 項目 | 量測值 |
|---|---|
| 裝置頁實體列的寬度 | 350px（**固定欄寬，與視窗大小無關**；800px 與 2000px 視窗量到的都是 350px） |
| 網址最長的不可斷字串 | `{subdomain 尾段}.woowtech.io/api/webhook/mcp_{32 hex}` = 69 字元 / 520px |
| 該列需要的寬度 | 584px（溢出 234px） |
| 名稱欄實得寬度 | 24px |

瀏覽器只在 `-` 後面斷行，而網址從最後一個 `-` 到結尾是一整段 69 字元；
`hui-generic-entity-row` 的狀態又沒有 `overflow-wrap: anywhere`，那一段就撐開整列，
把名稱欄（`flex: 1 1 30%` + `text-overflow: ellipsis`）壓到 24px。
**縮短名稱救不了**——實測名稱改成單一字元，名稱欄仍然是 24px，因為它拿到多少寬度
只取決於狀態的最小寬度。

曾經改成短標籤（`Open to copy`）換取名稱完整，但那讓使用者在 UI 上**完全拿不到網址**：
這個 HA 版本的 more-info 對話框沒有屬性區（只有狀態／歷史／logbook），屬性等於隱形。
權衡後**以完整網址的可見性為優先**。

想讓桌面那一列也不溢出，用主題或 card-mod 補上前端缺的那行 CSS（實測溢出歸零，
名稱欄回到 62px）：

```css
hui-generic-entity-row { overflow-wrap: anywhere; }
```
