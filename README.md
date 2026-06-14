# woow-paas-smart-home

這是 homeassitant custom component 會和 woow-paas-platform 整合

使用 Config entries 設定一個 woow paas smart home
1. 和 woow-paas-platform 做 Oauth 串接，登入 woow-paas-platform
2. 登入後，使用者會先選一個 workspace 再選一個 smart home ，然後就會回傳 Cloudflare Tunnel 讓 HA custom component Token 自動部署 cloudflared 進程。

## Security Access routes

選擇 Security Access 產品時，每一條 application route 會對應一個獨立的 URL sensor，並隨 PaaS 端的 route 異動在 30 秒輪詢內自動增減（免 reload）。

⚠️ **修改 route 的 hostname（subdomain prefix）等同重建**：PaaS 端的 hostname 是不可變的識別錨點，改名在後端是「刪除舊 route + 建立新 route」（產生新的 route id）。因此在 PaaS 改 route hostname 後，HA 端原本的 sensor 會變成 unavailable、並出現一個全新的 sensor，**該 route 的歷史資料與參照舊 entity_id 的 automation／dashboard 不會延續**。若只是調整 `service_url`、`is_protected` 等其他欄位則 route id 不變，sensor 會原地更新、保留歷史。