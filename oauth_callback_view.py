"""woowtech OAuth2 callback view (replaces my.home-assistant.io relay).

Lands the OAuth redirect directly on the local HA instance and renders the
woowtech page, then resumes the config flow using HA's JWT state validation.
The return destination is this instance's origin, never a stored hostname.
"""
from __future__ import annotations

from typing import Any

from aiohttp import web

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.helpers import http
from homeassistant.helpers.config_entry_oauth2_flow import _decode_jwt

from .const import (
    CALLBACK_AUTO_RETURN_MODE,
    CALLBACK_LINGER_SECONDS,
    DATA_CALLBACK_VIEW_REGISTERED,
    WOOW_AUTH_CALLBACK_PATH,
)


# Material Design Icons, same pinned version as the woowtech website.
_SVG_CUBE = '<span class="mdi mdi-cloud-outline" aria-hidden="true"></span>'
_SVG_HOME = '<span class="mdi mdi-home-assistant" aria-hidden="true"></span>'
_SVG_CHECK = '<span class="mdi mdi-check-circle-outline" aria-hidden="true"></span>'
_SVG_X = '<span class="mdi mdi-alert-circle-outline" aria-hidden="true"></span>'
_SVG_PENCIL = '<span class="mdi mdi-pencil-outline" aria-hidden="true"></span>'

_VARIANTS = {
    "pending": {
        "badge_text": "授權回應已收到",
        "title": "請返回 Home Assistant 確認連結結果",
        "subtitle": "Home Assistant 正在完成授權流程。請回到整合頁確認是否成功連結。",
        "pill_icon": _SVG_CHECK,
        "pill_text": "連結結果以整合頁顯示為準",
        "primary_text": "返回整合頁確認",
        "secondary": "",
    },
    "success": {
        "badge_text": "已收到回應",
        "title": "已收到授權回應",
        "subtitle": (
            "請回到 Home Assistant 繼續選擇工作區與服務，完成連線設定。"
            "連線狀態會在設定完成後顯示。"
        ),
        "pill_icon": _SVG_CHECK,
        "pill_text": "正在返回整合頁面…",
        "primary_text": "立即返回整合頁",
        "secondary": "",
    },
    "error": {
        "badge_text": "授權失敗",
        "title": "Home Assistant 連結失敗",
        "subtitle": "授權未完成或連結已逾時。請回到 Home Assistant 重新加入整合。",
        "pill_icon": _SVG_X,
        "pill_text": "授權遭拒或已逾時",
        "primary_text": "返回 Home Assistant",
        "secondary": (
            '<button class="woow-btn-text" id="woow-secondary-btn" type="button">'
            "關閉視窗</button>"
        ),
    },
}

_STYLE = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#eff1f5;padding:24px 16px;color:#212121;font:15px/1.6 'Outfit','Noto Sans TC',system-ui,sans-serif}
.woow-card{width:100%;max-width:480px;background:#fff;border:1px solid rgba(33,33,33,.08);border-radius:20px;padding:32px;box-shadow:0 4px 16px rgba(33,33,33,.06)}
.woow-brand{font:600 28px 'Poppins',sans-serif;letter-spacing:-.04em;color:#6183fc}
.woow-eyebrow{font-size:13px;color:#646262;margin:4px 0 24px}
.woow-icons{display:flex;align-items:center;gap:16px;color:#6183fc;font-size:32px;margin-bottom:20px}
.woow-icons .mdi-link-variant{font-size:22px}
.woow-badge{font-size:13px;color:#646262}
.woow-title{font:600 24px/1.4 'Poppins','Noto Sans TC',sans-serif;letter-spacing:-.02em;margin:8px 0}
.woow-subtitle{font-size:15px;color:#646262}
.woow-pill{display:flex;align-items:center;gap:8px;background:#eef2ff;border-radius:12px;padding:12px 16px;margin-top:20px;font-size:14px}
.woow-pill .mdi{font-size:22px;color:#6183fc}
body[data-status='error'] .woow-pill{background:#fff1f0;color:#b42318}
body[data-status='error'] .woow-pill .mdi{color:#b42318}
.woow-section{border-top:1px solid rgba(33,33,33,.08);margin-top:24px;padding-top:20px}
.woow-label,.woow-help{font-size:13px;color:#646262}
.woow-url{display:block;overflow-wrap:anywhere;font-size:14px;margin:8px 0;color:#212121}
.woow-actions{display:flex;flex-wrap:wrap;gap:12px;margin-top:24px}
.woow-actions button{border:0;border-radius:999px;min-height:44px;padding:12px 20px;cursor:pointer;font:600 15px 'Poppins','Noto Sans TC',sans-serif}
.woow-btn-primary{background:#6183fc;color:white}
.woow-btn-primary:hover{background:#3f63e8}
.woow-btn-text{background:#eff1f5;color:#212121}
button:focus-visible{outline:3px solid #3f63e8;outline-offset:3px}
@media(max-width:480px){.woow-card{padding:24px}.woow-title{font-size:22px}}
"""

_BODY_TEMPLATE = (
    '<main class="woow-card"><div class="woow-brand">woowtech</div>'
    '<p class="woow-eyebrow">智慧家庭連結</p>'
    '<div class="woow-icons">{cube}<span class="mdi mdi-link-variant" aria-hidden="true"></span>{home}</div>'
    '<div class="woow-badge">{badge_text}</div><h1 class="woow-title">{title}</h1>'
    '<p class="woow-subtitle">{subtitle}</p>'
    '<div class="woow-pill" role="status">{pill_icon}<span id="woow-pill-text">{pill_text}</span></div>'
    '<div class="woow-section"><div class="woow-label">Home Assistant 網址</div>'
    '<span class="woow-url" id="woow-ha-url"></span>'
    '<div class="woow-help">回到這台 Home Assistant 繼續設定。</div></div>'
    '<div class="woow-actions"><button class="woow-btn-primary" id="woow-primary-btn" type="button">{primary_text}</button>'
    '{secondary}</div></main>'
)

_SCRIPT = """
(function(){
  var status=document.body.getAttribute('data-status');
  // The callback already landed on the correct HA origin. A previously stored
  // hostname must never silently redirect this flow to another installation.
  var haUrl=location.origin;
  document.getElementById('woow-ha-url').textContent=haUrl;
  var timer=null;
  function finishReturn(){
    if(timer){clearInterval(timer);}
    try{window.close();}catch(e){}
    location.replace(haUrl+'/config/integrations/dashboard');
  }
  document.getElementById('woow-primary-btn').addEventListener('click',finishReturn);
  var secondary=document.getElementById('woow-secondary-btn');
  if(secondary){secondary.addEventListener('click',function(){try{window.close();}catch(e){}});}
  if(status==='success'){
    if(WOOW_INSTANT){try{window.close();}catch(e){}}
    var secs=WOOW_LINGER;
    var pill=document.getElementById('woow-pill-text');
    timer=setInterval(function(){
      secs-=1; pill.textContent='正在返回整合頁面…（'+secs+'）';
      if(secs<=0){finishReturn();}
    },1000);
  }
})();
"""


def _render_page(
    status: str,
    *,
    instant: bool | None = None,
    linger: int | None = None,
) -> str:
    """Render the woowtech page for `status` ('pending', 'success' or 'error').

    Auto-return behaviour is injected as JS globals WOOW_INSTANT / WOOW_LINGER.
    Both default to the const CALLBACK_AUTO_RETURN_MODE / CALLBACK_LINGER_SECONDS;
    pass them explicitly to override (e.g. in tests).
    """
    if instant is None:
        instant = CALLBACK_AUTO_RETURN_MODE == "instant"
    if linger is None:
        linger = CALLBACK_LINGER_SECONDS
    variant = _VARIANTS[status if status in _VARIANTS else "success"]
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
    safe_status = status
    config_js = (
        f"var WOOW_INSTANT={'true' if instant else 'false'};"
        f"var WOOW_LINGER={int(linger)};"
    )
    return (
        "<!DOCTYPE html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>連結 Home Assistant｜woowtech</title>"
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@500;600&amp;family=Outfit:wght@400;500;600&amp;family=Noto+Sans+TC:wght@400;500;600;700&amp;display=swap">'
        '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css">'
        f"<style>{_STYLE}</style></head>"
        f"<body data-status=\"{safe_status}\">{body}"
        f"<script>{config_js}{_SCRIPT}</script></body></html>"
    )


class WoowOAuth2CallbackView(http.HomeAssistantView):
    """Branded OAuth2 callback that resumes the config flow on the local HA."""

    requires_auth = False
    url = WOOW_AUTH_CALLBACK_PATH
    name = "woow_paas_smart_home:oauth_callback"

    async def get(self, request: web.Request) -> web.Response:
        """Receive the authorization code/error and resume the config flow."""
        # Reject ambiguous callback shapes before consuming any pending flow.
        # MultiDict's [] lookup would otherwise silently choose one value.
        query = request.query
        state_count = len(query.getall("state", []))
        code_count = len(query.getall("code", []))
        error_count = len(query.getall("error", []))
        if state_count != 1 or (code_count, error_count) not in {(1, 0), (0, 1)}:
            return _html(_render_page("error"), status=400)

        hass = request.app[http.KEY_HASS]
        state = _decode_jwt(hass, query["state"])
        if (not isinstance(state, dict)
                or not isinstance(state.get("flow_id"), str)
                or not state["flow_id"]):
            return _html(_render_page("error"), status=400)

        user_input: dict[str, Any] = {"state": state}
        if "code" in query:
            user_input["code"] = query["code"]
        elif "error" in query:
            user_input["error"] = query["error"]
        else:
            return _html(_render_page("error"), status=400)

        try:
            result = await hass.config_entries.flow.async_configure(
                flow_id=state["flow_id"], user_input=user_input
            )
        except UnknownFlow:
            return _html(_render_page("error"), status=400)
        result_type = result.get("type")
        reauth_done = (
            result_type in (FlowResultType.ABORT, "abort")
            and result.get("reason") == "reauth_successful"
        )
        rejected = result_type in (FlowResultType.ABORT, "abort") and not reauth_done
        # An external-step response only acknowledges code delivery. The token
        # exchange and workspace selection continue in Home Assistant's flow.
        status = (
            "error" if "error" in user_input or rejected
            else "success" if reauth_done
            else "pending"
        )
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
