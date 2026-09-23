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
sensor 之外會再多兩顆：

| 實體 | state | 預設 |
|---|---|---|
| `MCP URL` | `Open to copy`（有可用位址時） | 顯示在診斷區 |
| `MCP Connect URL` | **完整連線位址** `https://{subdomain}.woowtech.io/api/webhook/mcp_{32 hex}` | **停用** |

`MCP URL` 帶三個屬性：`connect_url`（同上的完整位址）、`webhook_id`、`base_url`。

三個條件都成立才有值，否則兩顆一起為空：`ha_mcp_tools` 的 config entry 已載入、
它的 `enable_webhook` 選項不是 `False`（local-only 模式下端點不存在）、tunnel 已連上。

### 怎麼拿到那串網址

```yaml
# 模板／自動化（不必啟用第二顆）
{{ state_attr('sensor.<裝置>_mcp_url', 'connect_url') }}

# 儀表板上直接顯示、可框選複製
type: markdown
content: "{{ state_attr('sensor.<裝置>_mcp_url', 'connect_url') }}"
```

想讓它成為一級 entity（`states()` 直接拿得到、詳細視窗看得到），到裝置頁的
**「未啟用的實體」**把 `MCP Connect URL` 啟用即可。啟用後它會多出一列，而那一列的
名稱會被擠壓——原因見下。

⚠️ **這個值是憑證。** `ha_mcp_tools` 預設 `webhook_auth=none`，網址本身就是進入 MCP
server 的鑰匙。它會進 recorder 歷史，也會出現在任何顯示它的儀表板。

### 為什麼要拆成兩顆

因為 Home Assistant 的裝置頁塞不下這串網址，硬塞會把**名稱擠到只剩一個字**。實測：

| 項目 | 量測值 |
|---|---|
| 裝置頁實體列的寬度 | 350px（**固定欄寬，與視窗大小無關**；800px 與 2000px 視窗量到的都是 350px） |
| 該列可用的內容寬 | 約 310px |
| 完整 URL 最長的不可斷字串 | `{subdomain 尾段}.woowtech.io/api/webhook/mcp_{32 hex}` = 69 字元 / 520px |
| 放完整 URL 時該列需要的寬度 | 584px（溢出 234px），名稱欄實得 **24px** |
| 放 36 字元 webhook id 時 | 不溢出，但名稱欄只有 32px（`MCP URL` 需 84px），仍被截 |
| 放短標籤 `Open to copy` 時 | 不溢出，名稱欄 **完整顯示** ✅ |

瀏覽器只在 `-` 後面斷行，而 URL 從最後一個 `-` 到結尾是一整段 69 字元；
`hui-generic-entity-row` 的狀態又沒有 `overflow-wrap: anywhere`，那一段就撐開整列，
把名稱欄（`flex: 1 1 30%` + `text-overflow: ellipsis`）壓到 24px。

**縮短名稱救不了**——實測把名稱改成單一字元，名稱欄仍然是 24px，因為它拿到多少寬度
只取決於狀態的最小寬度。

**為什麼第二顆用「停用」而不是「隱藏」**：實測 `entity_registry_visible_default = False`
的 entity 在裝置頁**仍然會被列出來**（名稱後面加「已隱藏」），照樣溢出 234px，等於沒
解決。停用的 entity 才會被收進「未啟用的實體」摺疊區、不佔一列。

想讓啟用後的那一列也不溢出，可用主題或 card-mod 補上前端缺的那行 CSS（實測溢出歸零，
但名稱欄仍只有 62px、還是會被截）：

```css
hui-generic-entity-row { overflow-wrap: anywhere; }
```
