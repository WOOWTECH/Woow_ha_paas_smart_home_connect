# woow-paas-smart-home

這是 homeassitant custom component 會和 woow-paas-platform 整合

使用 Config entries 設定一個 woow paas smart home
1. 和 woow-paas-platform 做 Oauth 串接，登入 woow-paas-platform
2. 登入後，使用者會先選一個 workspace 再選一個 smart home ，然後就會回傳 Cloudflare Tunnel 讓 HA custom component Token 自動部署 cloudflared 進程。