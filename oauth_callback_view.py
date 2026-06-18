"""WOOW-branded OAuth2 callback view (replaces my.home-assistant.io relay).

Lands the OAuth redirect directly on the local HA instance and renders the
"方向 A — 忠於原版" branded page, then resumes the config flow exactly like HA
Core's OAuth2AuthorizeCallbackView — reusing _decode_jwt + async_configure.
Only the rendered page is new; the token never leaves the local instance.
"""
from __future__ import annotations

from typing import Any

from aiohttp import web

from homeassistant.core import HomeAssistant
from homeassistant.helpers import http
from homeassistant.helpers.config_entry_oauth2_flow import _decode_jwt

from .const import DATA_CALLBACK_VIEW_REGISTERED, WOOW_AUTH_CALLBACK_PATH


_SVG_CUBE = (
    '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#3D5AF1"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2'
    ' 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"></path>'
    '<path d="m3.3 7 8.7 5 8.7-5"></path><path d="M12 22V12"></path></svg>'
)
_SVG_HOME = (
    '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#18BCF2"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M3 10.5 12 3l9 7.5"></path><path d="M5 9.5V21h14V9.5"></path>'
    '<path d="M9 21v-6h6v6"></path></svg>'
)
_SVG_CHECK = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M20 6 9 17l-5-5"></path></svg>'
)
_SVG_X = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M18 6 6 18"></path><path d="M6 6l12 12"></path></svg>'
)
_SVG_PENCIL = (
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 20h9"></path>'
    '<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"></path></svg>'
)

_VARIANTS = {
    "success": {
        "badge_text": "已授權",
        "title": "已成功連結 Home Assistant",
        "subtitle": (
            "Woow 已完成 OAuth 授權，系統將自動帶你返回 Home Assistant 整合頁面。"
            "你也可以立即返回。"
        ),
        "pill_icon": _SVG_CHECK,
        "pill_text": "正在返回整合頁面…",
        "primary_text": "立即返回整合頁",
        "secondary": "",
    },
    "error": {
        "badge_text": "授權失敗",
        "title": "Home Assistant 連結失敗",
        "subtitle": "授權未完成或已逾時。請確認下方的 Home Assistant 網址後重新嘗試。",
        "pill_icon": _SVG_X,
        "pill_text": "授權遭拒或已逾時",
        "primary_text": "重新嘗試",
        "secondary": (
            '<button class="woow-btn-text" id="woow-secondary-btn" type="button">'
            "關閉視窗</button>"
        ),
    },
}

_STYLE = (
    "*{box-sizing:border-box}"
    "body{margin:0;min-height:100vh;display:flex;align-items:center;"
    "justify-content:center;background:#eef0f4;padding:24px;color:#14161b;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
    "'PingFang TC','Microsoft JhengHei',sans-serif}"
    ".woow-card{width:100%;max-width:460px;background:#fff;border:1px solid #e8eaef;"
    "border-radius:18px;box-shadow:0 1px 2px rgba(17,24,39,.04),"
    "0 4px 14px rgba(17,24,39,.035);overflow:hidden}"
    ".woow-header{position:relative;height:188px;overflow:hidden;"
    "background:linear-gradient(135deg, #5b73ff 0%, #3d5af1 60%, #2e45d6 100%)}"
    ".woow-header .dots{position:absolute;inset:0;background-size:18px 18px;"
    "opacity:.55;background-image:radial-gradient(rgba(255,255,255,.16) 1px,"
    "transparent 1px)}"
    ".woow-header .glow{position:absolute;top:-50px;right:-40px;width:200px;"
    "height:200px;border-radius:50%;"
    "background:radial-gradient(circle, rgba(255,255,255,.22), transparent 70%)}"
    ".woow-badge{position:absolute;top:14px;left:14px;display:inline-flex;"
    "align-items:center;gap:7px;background:rgba(255,255,255,.94);font-size:12px;"
    "font-weight:700;padding:6px 12px;border-radius:999px}"
    ".woow-badge .dot{width:7px;height:7px;border-radius:50%}"
    ".woow-icons{position:absolute;inset:0;display:flex;align-items:center;"
    "justify-content:center;gap:20px}"
    ".woow-tile{width:64px;height:64px;border-radius:16px;background:#fff;"
    "box-shadow:0 10px 28px rgba(20,22,40,.25);display:flex;align-items:center;"
    "justify-content:center}"
    ".woow-pulse{display:flex;gap:5px;align-items:center}"
    ".woow-pulse span{width:6px;height:6px;border-radius:50%;background:#fff;"
    "animation:pulseDot 1.4s ease-in-out infinite}"
    ".woow-pulse span:nth-child(2){animation-delay:.2s}"
    ".woow-pulse span:nth-child(3){animation-delay:.4s}"
    "@keyframes pulseDot{0%,100%{opacity:.3}50%{opacity:1}}"
    ".woow-body{padding:24px 26px 26px}"
    ".woow-title{font-size:21px;font-weight:800;letter-spacing:-.02em;"
    "line-height:1.25}"
    ".woow-subtitle{margin-top:8px;font-size:14.5px;line-height:1.6;color:#3c4150}"
    ".woow-pill{margin-top:16px;display:inline-flex;align-items:center;gap:8px;"
    "padding:8px 13px;border-radius:10px;font-size:13.5px;font-weight:600}"
    ".woow-section{margin-top:18px;padding-top:18px;border-top:1px solid #eef0f4}"
    ".woow-label{font-size:11.5px;font-weight:700;letter-spacing:.07em;color:#8a909e}"
    ".woow-url-row{margin-top:8px;display:flex;align-items:center;gap:10px}"
    ".woow-url{flex:1 1 0;min-width:0;font-size:14px;color:#3b69ef;"
    "word-break:break-all;font-family:'JetBrains Mono',ui-monospace,monospace}"
    ".woow-input{flex:1 1 0;min-width:0;font-size:14px;padding:8px 10px;"
    "border:1px solid #e8eaef;border-radius:10px;color:#14161b;"
    "font-family:'JetBrains Mono',ui-monospace,monospace}"
    ".woow-iconbtn{flex:0 0 auto;width:34px;height:34px;display:inline-flex;"
    "align-items:center;justify-content:center;border:1px solid #e8eaef;"
    "background:#fff;border-radius:10px;color:#8a909e;cursor:pointer;"
    "box-shadow:0 1px 1px rgba(17,24,39,.04)}"
    ".woow-help{margin-top:8px;font-size:12px;color:#8a909e}"
    ".woow-actions{margin-top:20px;display:flex;gap:10px}"
    ".woow-btn-primary{appearance:none;border:none;cursor:pointer;"
    "font-family:inherit;font-weight:700;font-size:14px;color:#fff;"
    "background:#3d5af1;border-radius:10px;padding:11px 18px;"
    "box-shadow:0 4px 12px rgba(61,90,241,.32)}"
    ".woow-btn-text{appearance:none;border:none;background:transparent;"
    "cursor:pointer;font-family:inherit;font-weight:600;font-size:14px;"
    "color:#3c4150;border-radius:10px;padding:11px 14px}"
    "body[data-status='success'] .woow-badge{color:#157f43}"
    "body[data-status='success'] .woow-badge .dot{background:#1fa055}"
    "body[data-status='success'] .woow-pill{background:#e7f6ec;color:#157f43}"
    "body[data-status='error'] .woow-badge{color:#cf3439}"
    "body[data-status='error'] .woow-badge .dot{background:#e5484d}"
    "body[data-status='error'] .woow-pill{background:#fdecec;color:#cf3439}"
)

_BODY_TEMPLATE = (
    '<div class="woow-card"><div class="woow-header">'
    '<div class="dots"></div><div class="glow"></div>'
    '<span class="woow-badge"><span class="dot"></span>{badge_text}</span>'
    '<div class="woow-icons"><div class="woow-tile">{cube}</div>'
    '<div class="woow-pulse"><span></span><span></span><span></span></div>'
    '<div class="woow-tile">{home}</div></div></div>'
    '<div class="woow-body">'
    '<div class="woow-title">{title}</div>'
    '<div class="woow-subtitle">{subtitle}</div>'
    '<span class="woow-pill">{pill_icon}<span id="woow-pill-text">{pill_text}</span></span>'
    '<div class="woow-section"><div class="woow-label">HOME ASSISTANT 執行個體</div>'
    '<div class="woow-url-row" id="woow-view-row">'
    '<span class="woow-url" id="woow-ha-url"></span>'
    '<button class="woow-iconbtn" id="woow-edit-btn" type="button" title="編輯網址">{pencil}</button>'
    '</div>'
    '<div class="woow-url-row" id="woow-edit-row" style="display:none">'
    '<input class="woow-input" id="woow-url-input" type="url" inputmode="url" />'
    '<button class="woow-iconbtn" id="woow-save-btn" type="button" title="儲存">{check}</button>'
    '<button class="woow-iconbtn" id="woow-cancel-btn" type="button" title="取消">{x}</button>'
    '</div>'
    '<div class="woow-help">此網址僅儲存於你的瀏覽器。</div></div>'
    '<div class="woow-actions">'
    '<button class="woow-btn-primary" id="woow-primary-btn" type="button">{primary_text}</button>'
    '{secondary}</div></div></div>'
)

_SCRIPT = (
    "(function(){"
    "var status=document.body.getAttribute('data-status');"
    "var KEY='woow_ha_url';"
    "function clean(u){return (u||'').trim().replace(/\\/+$/,'');}"
    "var stored=null;try{stored=localStorage.getItem(KEY);}catch(e){}"
    "var haUrl=clean(stored)||clean(location.origin);"
    "var urlEl=document.getElementById('woow-ha-url');"
    "if(urlEl){urlEl.textContent=haUrl;}"
    "function returnToIntegrations(){location.href=haUrl+'/config/integrations/dashboard';}"
    "var viewRow=document.getElementById('woow-view-row');"
    "var editRow=document.getElementById('woow-edit-row');"
    "var input=document.getElementById('woow-url-input');"
    "var editBtn=document.getElementById('woow-edit-btn');"
    "var saveBtn=document.getElementById('woow-save-btn');"
    "var cancelBtn=document.getElementById('woow-cancel-btn');"
    "if(editBtn){editBtn.addEventListener('click',function(){input.value=haUrl;"
    "viewRow.style.display='none';editRow.style.display='flex';input.focus();});}"
    "if(cancelBtn){cancelBtn.addEventListener('click',function(){"
    "editRow.style.display='none';viewRow.style.display='flex';});}"
    "if(saveBtn){saveBtn.addEventListener('click',function(){var v=clean(input.value)||haUrl;"
    "haUrl=v;try{localStorage.setItem(KEY,v);}catch(e){}if(urlEl){urlEl.textContent=v;}"
    "editRow.style.display='none';viewRow.style.display='flex';});}"
    "var primary=document.getElementById('woow-primary-btn');"
    "var secondary=document.getElementById('woow-secondary-btn');"
    "if(status==='success'){"
    "try{window.close();}catch(e){}"
    "var secs=5;var pill=document.getElementById('woow-pill-text');"
    "var timer=setInterval(function(){secs-=1;"
    "if(pill){pill.textContent='正在返回整合頁面…（'+secs+'）';}"
    "if(secs<=0){clearInterval(timer);returnToIntegrations();}},1000);"
    "if(primary){primary.addEventListener('click',function(){clearInterval(timer);"
    "try{window.close();}catch(e){}returnToIntegrations();});}"
    "}else{"
    "if(primary){primary.addEventListener('click',function(){"
    "try{window.close();}catch(e){}returnToIntegrations();});}"
    "if(secondary){secondary.addEventListener('click',function(){"
    "try{window.close();}catch(e){}});}"
    "}})();"
)


def _render_page(status: str) -> str:
    """Render the 方向 A branded page for `status` ('success' or 'error')."""
    variant = _VARIANTS["error" if status == "error" else "success"]
    body = _BODY_TEMPLATE.format(
        badge_text=variant["badge_text"],
        title=variant["title"],
        subtitle=variant["subtitle"],
        pill_icon=variant["pill_icon"],
        pill_text=variant["pill_text"],
        primary_text=variant["primary_text"],
        secondary=variant["secondary"],
        cube=_SVG_CUBE,
        home=_SVG_HOME,
        pencil=_SVG_PENCIL,
        check=_SVG_CHECK,
        x=_SVG_X,
    )
    safe_status = "error" if status == "error" else "success"
    return (
        "<!DOCTYPE html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Woow · OAuth 授權回調</title>"
        f"<style>{_STYLE}</style></head>"
        f"<body data-status=\"{safe_status}\">{body}"
        f"<script>{_SCRIPT}</script></body></html>"
    )


class WoowOAuth2CallbackView(http.HomeAssistantView):
    """Branded OAuth2 callback that resumes the config flow on the local HA."""

    requires_auth = False
    url = WOOW_AUTH_CALLBACK_PATH
    name = "woow_paas_smart_home:oauth_callback"

    async def get(self, request: web.Request) -> web.Response:
        """Receive the authorization code/error and resume the config flow."""
        if "state" not in request.query:
            return _html(_render_page("error"), status=400)

        hass = request.app[http.KEY_HASS]
        state = _decode_jwt(hass, request.query["state"])
        if state is None:
            return _html(_render_page("error"), status=400)

        user_input: dict[str, Any] = {"state": state}
        if "code" in request.query:
            user_input["code"] = request.query["code"]
        elif "error" in request.query:
            user_input["error"] = request.query["error"]
        else:
            return _html(_render_page("error"), status=400)

        await hass.config_entries.flow.async_configure(
            flow_id=state["flow_id"], user_input=user_input
        )
        status = "error" if "error" in user_input else "success"
        return _html(_render_page(status))


def _html(body: str, status: int = 200) -> web.Response:
    return web.Response(text=body, content_type="text/html", status=status)


def async_register_woow_callback_view(hass: HomeAssistant) -> None:
    """Register the callback view exactly once (idempotent).

    Must be available before the authorize redirect returns, so it is called
    lazily from config_flow.async_step_user (first-time setup + reauth) and
    from async_setup (existing entries on restart).
    """
    if hass.data.get(DATA_CALLBACK_VIEW_REGISTERED):
        return
    hass.http.register_view(WoowOAuth2CallbackView())
    hass.data[DATA_CALLBACK_VIEW_REGISTERED] = True
