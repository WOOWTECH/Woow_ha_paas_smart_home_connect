# 自訂 OAuth Callback 跳轉頁 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 component 註冊自家 callback view、override `redirect_uri`，把 OAuth callback 從 `my.home-assistant.io` 換成落在本機 HA、由 component 渲染的「方向 A — 忠於原版」WOOW 品牌頁。

**Architecture:** override `WoowPaasOAuth2.redirect_uri` 略過 HA Core 的 `my` 分支，指向 `{HA-Frontend-Base}/auth/external/woow/callback`；新增 `WoowOAuth2CallbackView`（鏡像 HA Core `OAuth2AuthorizeCallbackView`，**重用** `_decode_jwt` + `async_configure`），在 `config_flow.async_step_user` 與 `__init__.async_setup` 兩處 idempotent 註冊。token 全程不離本機。

**Tech Stack:** Python 3.13 / Home Assistant custom component、aiohttp web view、pytest（HA core `hass` fixture）、vanilla JS（callback 頁）。

## Global Constraints

- 跨 repo 契約常數（兩端逐字一致，改值屬 breaking）：path = `/auth/external/woow/callback`（component `const.py:WOOW_AUTH_CALLBACK_PATH`；paas `OAuthClient.HA_CALLBACK_PATH`）；`client_id = woow-ha-smart-home`。
- callback 頁**不載 React、不依賴外部 CSS/字型 CDN**；自包含 HTML + inline `<style>` + vanilla JS；`@keyframes pulseDot{0%,100%{opacity:.3}50%{opacity:1}}`；mono 字型 `"JetBrains Mono", ui-monospace, monospace`。
- 只重用 HA Core `_decode_jwt` / `async_configure`，**不 fork / 不複製** HA Core OAuth helper。
- 測試在 HA devcontainer（ha-venv）內跑 pytest，非 host；不安裝 PHCC。package import 路徑為 `custom_components.woow_paas_smart_home`。
- commit message：`{type}: {中文訊息}`（type 英文小寫），不加 AI 生成註記。
- 文案逐字（成功）：標題「已成功連結 Home Assistant」、內文「Woow 已完成 OAuth 授權，系統將自動帶你返回 Home Assistant 整合頁面。你也可以立即返回。」、pill「正在返回整合頁面…」、主鈕「立即返回整合頁」、badge「已授權」。
- 文案逐字（失敗）：標題「Home Assistant 連結失敗」、內文「授權未完成或已逾時。請確認下方的 Home Assistant 網址後重新嘗試。」、pill「授權遭拒或已逾時」、主鈕「重新嘗試」、次鈕「關閉視窗」、badge「授權失敗」、helper「此網址僅儲存於你的瀏覽器。」。
- 設計來源/視覺驗收基準：`docs/superpowers/specs/2026-06-18-custom-oauth-callback-page-design.md` §4.5 + `assets/callback-design-success.png`、`assets/callback-design-error.png`。

---

### Task 1: `redirect_uri` override（const + oauth2）

**Files:**
- Modify: `const.py`（檔尾新增常數）
- Modify: `oauth2.py`（`WoowPaasOAuth2` 新增 `redirect_uri` property）
- Test: `tests/test_oauth2.py`（新檔）

**Interfaces:**
- Produces:
  - `const.WOOW_AUTH_CALLBACK_PATH: str = "/auth/external/woow/callback"`
  - `const.DATA_CALLBACK_VIEW_REGISTERED: str = "woow_paas_smart_home_callback_view_registered"`
  - `WoowPaasOAuth2.redirect_uri -> str`（回 `{HA-Frontend-Base}{WOOW_AUTH_CALLBACK_PATH}`；無 request/header → `RuntimeError`）

- [ ] **Step 1: 寫失敗測試** — `tests/test_oauth2.py`

```python
"""Unit tests for WoowPaasOAuth2.redirect_uri override.

`hass` fixture comes from HA core's tests/conftest.py (wired in conftest.py).
The override mirrors HA Core's non-`my` branch but always targets the WOOW
callback path, so the my.home-assistant.io relay is never used.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import http as helpers_http

from custom_components.woow_paas_smart_home.const import WOOW_AUTH_CALLBACK_PATH
from custom_components.woow_paas_smart_home.oauth2 import create_implementation


def _request_with_base(base: str) -> MagicMock:
    request = MagicMock()
    request.headers = {"HA-Frontend-Base": base}
    return request


async def test_redirect_uri_targets_local_woow_callback(hass: HomeAssistant) -> None:
    token = helpers_http.current_request.set(_request_with_base("https://ha.example.com"))
    try:
        impl = create_implementation(hass)
        assert (
            impl.redirect_uri
            == f"https://ha.example.com{WOOW_AUTH_CALLBACK_PATH}"
        )
    finally:
        helpers_http.current_request.reset(token)


async def test_redirect_uri_never_uses_my_relay_even_when_my_loaded(
    hass: HomeAssistant,
) -> None:
    hass.config.components.add("my")
    token = helpers_http.current_request.set(_request_with_base("http://homeassistant.local:8123"))
    try:
        impl = create_implementation(hass)
        uri = impl.redirect_uri
        assert uri == f"http://homeassistant.local:8123{WOOW_AUTH_CALLBACK_PATH}"
        assert "my.home-assistant.io" not in uri
    finally:
        helpers_http.current_request.reset(token)


async def test_redirect_uri_raises_without_request(hass: HomeAssistant) -> None:
    impl = create_implementation(hass)
    with pytest.raises(RuntimeError):
        _ = impl.redirect_uri
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_oauth2.py -v`
Expected: FAIL — `AttributeError`/`ImportError`（`WOOW_AUTH_CALLBACK_PATH` 未定義 或 redirect_uri 仍回 my-relay）。

- [ ] **Step 3: 在 `const.py` 檔尾新增常數**

```python
# --- Custom OAuth2 callback (replaces my.home-assistant.io relay) ---
# Cross-repo contract constant: paas side OAuthClient.HA_CALLBACK_PATH must match
# this value byte-for-byte. Changing it is a cross-repo breaking change.
WOOW_AUTH_CALLBACK_PATH = "/auth/external/woow/callback"

# hass.data flag guarding idempotent callback-view registration.
DATA_CALLBACK_VIEW_REGISTERED = "woow_paas_smart_home_callback_view_registered"
```

- [ ] **Step 4: 在 `oauth2.py` 加 `redirect_uri` override**

在 import 區加入：
```python
from homeassistant.helpers import http
from homeassistant.helpers.config_entry_oauth2_flow import HEADER_FRONTEND_BASE
```
在 import 的 `from .const import (...)` 加入 `WOOW_AUTH_CALLBACK_PATH`。
在 `WoowPaasOAuth2` class 內（`extra_authorize_data` 之上或之下）新增：
```python
    @property
    def redirect_uri(self) -> str:
        """Return the WOOW-hosted callback on the local HA instance.

        Bypasses HA Core's my.home-assistant.io relay (async_get_redirect_uri
        returns MY_AUTH_CALLBACK_PATH whenever the `my` component is loaded) so
        the OAuth callback lands directly on a component-rendered WOOW page.
        Mirrors HA Core's non-`my` branch.
        """
        if (req := http.current_request.get()) is None:
            raise RuntimeError("No current request in context")
        if (ha_host := req.headers.get(HEADER_FRONTEND_BASE)) is None:
            raise RuntimeError("No header in request")
        return f"{ha_host}{WOOW_AUTH_CALLBACK_PATH}"
```

- [ ] **Step 5: 跑測試確認通過**

Run: `python -m pytest tests/test_oauth2.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 6: Commit**

```bash
git add const.py oauth2.py tests/test_oauth2.py
git commit -m "feat: redirect_uri 改指向本機自家 callback，不走 my.home-assistant.io"
```

---

### Task 2: Callback view 與「方向 A」品牌頁

**Files:**
- Create: `oauth_callback_view.py`
- Test: `tests/test_oauth_callback_view.py`（新檔）

**Interfaces:**
- Consumes: `const.WOOW_AUTH_CALLBACK_PATH`、`const.DATA_CALLBACK_VIEW_REGISTERED`（Task 1）；HA Core `_decode_jwt`、`hass.config_entries.flow.async_configure`、`homeassistant.helpers.http`（`HomeAssistantView`、`KEY_HASS`）。
- Produces:
  - `class WoowOAuth2CallbackView(http.HomeAssistantView)`，`url = WOOW_AUTH_CALLBACK_PATH`，`name = "woow_paas_smart_home:oauth_callback"`，`async def get(request) -> web.Response`
  - `async_register_woow_callback_view(hass) -> None`（idempotent）
  - `_render_page(status: str) -> str`（`status ∈ {"success","error"}`）

- [ ] **Step 1: 寫失敗測試** — `tests/test_oauth_callback_view.py`

```python
"""Unit tests for WoowOAuth2CallbackView.

Builds a mocked aiohttp request (query + app[KEY_HASS]) and drives get()
directly. A valid signed `state` is produced with HA Core's _encode_jwt so the
view's _decode_jwt reuse is exercised end to end. The flow is spied via
monkeypatched async_configure. `hass` comes from HA core's tests/conftest.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import http
from homeassistant.helpers.config_entry_oauth2_flow import _encode_jwt

from custom_components.woow_paas_smart_home.const import (
    DATA_CALLBACK_VIEW_REGISTERED,
)
from custom_components.woow_paas_smart_home.oauth_callback_view import (
    WoowOAuth2CallbackView,
    async_register_woow_callback_view,
)


def _request(hass: HomeAssistant, query: dict) -> MagicMock:
    request = MagicMock()
    request.query = query
    request.app = {http.KEY_HASS: hass}
    return request


def _spy_flow(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    spy = AsyncMock()
    monkeypatch.setattr(hass.config_entries.flow, "async_configure", spy)
    return spy


async def test_success_resumes_flow_and_renders_success_page(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _spy_flow(hass, monkeypatch)
    state = _encode_jwt(hass, {"flow_id": "flow-1", "redirect_uri": "https://ha/x"})

    resp = await WoowOAuth2CallbackView().get(
        _request(hass, {"state": state, "code": "the-code"})
    )

    assert resp.status == 200
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["flow_id"] == "flow-1"
    assert kwargs["user_input"] == {
        "state": {"flow_id": "flow-1", "redirect_uri": "https://ha/x"},
        "code": "the-code",
    }
    assert "已成功連結 Home Assistant" in resp.text
    assert "window.close" in resp.text


async def test_invalid_state_renders_error_400_and_skips_flow(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _spy_flow(hass, monkeypatch)

    resp = await WoowOAuth2CallbackView().get(
        _request(hass, {"state": "not-a-valid-jwt", "code": "x"})
    )

    assert resp.status == 400
    spy.assert_not_awaited()
    assert "Home Assistant 連結失敗" in resp.text


async def test_missing_state_renders_error_400(hass: HomeAssistant) -> None:
    resp = await WoowOAuth2CallbackView().get(_request(hass, {"code": "x"}))
    assert resp.status == 400
    assert "Home Assistant 連結失敗" in resp.text


async def test_missing_code_and_error_renders_400(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _spy_flow(hass, monkeypatch)
    state = _encode_jwt(hass, {"flow_id": "f", "redirect_uri": "x"})

    resp = await WoowOAuth2CallbackView().get(_request(hass, {"state": state}))

    assert resp.status == 400
    spy.assert_not_awaited()


async def test_user_rejected_resumes_with_error_and_renders_error_page(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _spy_flow(hass, monkeypatch)
    state = _encode_jwt(hass, {"flow_id": "f", "redirect_uri": "x"})

    resp = await WoowOAuth2CallbackView().get(
        _request(hass, {"state": state, "error": "access_denied"})
    )

    assert resp.status == 200
    assert spy.await_args.kwargs["user_input"]["error"] == "access_denied"
    assert "Home Assistant 連結失敗" in resp.text


async def test_register_view_is_idempotent(hass: HomeAssistant) -> None:
    hass.http = MagicMock()

    async_register_woow_callback_view(hass)
    async_register_woow_callback_view(hass)

    assert hass.http.register_view.call_count == 1
    assert hass.data.get(DATA_CALLBACK_VIEW_REGISTERED) is True


def test_rendered_page_carries_direction_a_design_tokens() -> None:
    """Regression-lock 方向 A 視覺指紋（色彩/SVG/keyframe/行為標記）。"""
    from custom_components.woow_paas_smart_home.oauth_callback_view import _render_page

    success = _render_page("success")
    error = _render_page("error")

    # header 漸層 + pulseDot keyframe
    assert "linear-gradient(135deg, #5b73ff 0%, #3d5af1 60%, #2e45d6 100%)" in success
    assert "@keyframes pulseDot" in success
    # 主按鈕色 + cube/home SVG path 指紋
    assert "#3d5af1" in success
    assert "M3 10.5 12 3l9 7.5" in success  # home icon
    # 行為標記：localStorage key、整合頁導回、倒數
    assert "woow_ha_url" in success
    assert "/config/integrations/dashboard" in success
    # 狀態文案分流
    assert "正在返回整合頁面" in success and "立即返回整合頁" in success
    assert "授權遭拒或已逾時" in error and "關閉視窗" in error
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_oauth_callback_view.py -v`
Expected: FAIL — `ModuleNotFoundError: ...oauth_callback_view`。

- [ ] **Step 3: 建立 `oauth_callback_view.py`（view + 註冊輔助 + render 骨架）**

```python
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
```

- [ ] **Step 4: 在 `oauth_callback_view.py` 加入「方向 A」品牌頁（樣式/SVG/JS/render）**

接在上方 import 之後、`WoowOAuth2CallbackView` 之前（或檔尾）加入：

```python
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
```

- [ ] **Step 5: 跑測試確認通過**

Run: `python -m pytest tests/test_oauth_callback_view.py -v`
Expected: PASS（7 passed）。

- [ ] **Step 6: 視覺自檢（人工，非阻塞）**

把 `_render_page("success")` / `_render_page("error")` 的輸出存成暫存 .html 用瀏覽器開，比對
`docs/superpowers/specs/assets/callback-design-success.png` / `-error.png`（卡片 460px、藍漸層 header、雙 icon tile、綠/紅 badge、pill、URL 欄、按鈕）。

- [ ] **Step 7: Commit**

```bash
git add oauth_callback_view.py tests/test_oauth_callback_view.py
git commit -m "feat: 新增 WOOW 品牌 OAuth callback view（方向 A 忠於原版）"
```

---

### Task 3: 註冊 callback view（config_flow + __init__）

**Files:**
- Modify: `config_flow.py`（`async_step_user`）
- Modify: `__init__.py`（`async_setup`）
- Test: `tests/test_callback_view_registration.py`（新檔）

**Interfaces:**
- Consumes: `oauth_callback_view.async_register_woow_callback_view`、`const.DATA_CALLBACK_VIEW_REGISTERED`（Task 2/1）。

- [ ] **Step 1: 寫失敗測試** — `tests/test_callback_view_registration.py`

```python
"""Callback view is registered at both lazy entry points.

First-time UI setup never calls async_setup, so config_flow.async_step_user
must register the view; restarts with existing entries register via async_setup.
Both paths are idempotent. `hass` comes from HA core's tests/conftest.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.woow_paas_smart_home import async_setup
from custom_components.woow_paas_smart_home.config_flow import ConfigFlow
from custom_components.woow_paas_smart_home.const import (
    DATA_CALLBACK_VIEW_REGISTERED,
)


async def test_async_setup_registers_callback_view(hass: HomeAssistant) -> None:
    hass.http = MagicMock()

    assert await async_setup(hass, {}) is True

    assert hass.data.get(DATA_CALLBACK_VIEW_REGISTERED) is True
    hass.http.register_view.assert_called_once()


async def test_config_flow_step_user_registers_callback_view(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass.http = MagicMock()
    # Stub the heavy OAuth external-step path; we only assert our registration ran.
    monkeypatch.setattr(
        "homeassistant.helpers.config_entry_oauth2_flow."
        "AbstractOAuth2FlowHandler.async_step_user",
        AsyncMock(return_value={"type": "external_step"}),
    )
    flow = ConfigFlow()
    flow.hass = hass

    await flow.async_step_user(None)

    assert hass.data.get(DATA_CALLBACK_VIEW_REGISTERED) is True
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_callback_view_registration.py -v`
Expected: FAIL（兩個測試的 `DATA_CALLBACK_VIEW_REGISTERED` 旗標皆為 None）。

- [ ] **Step 3: 在 `__init__.py` `async_setup` 註冊 view**

在 `__init__.py` import 區加：
```python
from .oauth_callback_view import async_register_woow_callback_view
```
在 `async_setup` 內、現有 `config_entry_oauth2_flow.async_register_implementation(...)` 之後加一行：
```python
    async_register_woow_callback_view(hass)
```

- [ ] **Step 4: 在 `config_flow.py` `async_step_user` 註冊 view**

在 `config_flow.py` import 區加：
```python
from .oauth_callback_view import async_register_woow_callback_view
```
在 `async_step_user` 內、`async_register_implementation(...)` 之後（`return await super().async_step_user(...)` 之前）加一行：
```python
        async_register_woow_callback_view(self.hass)
```

- [ ] **Step 5: 跑測試確認通過**

Run: `python -m pytest tests/test_callback_view_registration.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 6: Commit**

```bash
git add __init__.py config_flow.py tests/test_callback_view_registration.py
git commit -m "feat: 於 async_setup 與 config_flow 註冊 callback view（idempotent）"
```

---

### Task 4: CI 冒煙測試 — 鎖 HA Core 私有 helper

**Files:**
- Test: `tests/test_ha_core_jwt_contract.py`（新檔）

**Interfaces:**
- Consumes: HA Core `_encode_jwt` / `_decode_jwt`（view 重用的私有 helper）。

- [ ] **Step 1: 寫測試**（此為契約鎖，先寫即通過）— `tests/test_ha_core_jwt_contract.py`

```python
"""Smoke-lock HA Core's private state-JWT helpers used by the callback view.

WoowOAuth2CallbackView reuses _decode_jwt; if a HA upgrade changes the helper's
name or round-trip behaviour, this fails loudly so we catch drift in CI.
`hass` comes from HA core's tests/conftest.py.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import _decode_jwt, _encode_jwt


async def test_state_jwt_roundtrip(hass: HomeAssistant) -> None:
    payload = {"flow_id": "abc", "redirect_uri": "https://ha.example.com/x"}

    token = _encode_jwt(hass, payload)
    decoded = _decode_jwt(hass, token)

    assert decoded is not None
    assert decoded["flow_id"] == "abc"
    assert decoded["redirect_uri"] == "https://ha.example.com/x"


async def test_decode_rejects_tampered_token(hass: HomeAssistant) -> None:
    # Seed the per-hass secret, then decode garbage -> None (the CSRF backstop).
    _encode_jwt(hass, {"flow_id": "x"})
    assert _decode_jwt(hass, "tampered.jwt.value") is None
```

- [ ] **Step 2: 跑測試確認通過**

Run: `python -m pytest tests/test_ha_core_jwt_contract.py -v`
Expected: PASS（2 passed）。若 FAIL → HA 版本改了私有 helper，需同步調整 view。

- [ ] **Step 3: 全套件 + hassfest**

Run（在 devcontainer / ha-venv 內）：
```bash
# 全套件：在 component 目錄
python -m pytest tests/ -v
# manifest/結構驗證：在 repo root（woow-components/，含 script/ 與 homeassistant/）
cd <repo-root>/config/custom_components/woow_paas_smart_home/../../..  # = woow-components
python -m script.hassfest --integration-path config/custom_components/woow_paas_smart_home
```
Expected: 全 PASS；hassfest 無錯（若該版 hassfest 不支援 `--integration-path`，以團隊慣用的 `python -m script.hassfest` 全量驗證取代，確認本元件無新錯）。

- [ ] **Step 4: Commit**

```bash
git add tests/test_ha_core_jwt_contract.py
git commit -m "test: 冒煙鎖 HA Core state-JWT 私有 helper 防版本漂移"
```

---

### Task 5: E2E 驗證（gated：需 paas-platform 先發 stg）

> **依賴**：paas-platform agent 完成 `check_redirect_uri` 放寬（path 鎖 `/auth/external/woow/callback`、公網強制 https / http 限本機、移除 my.home-assistant.io 舊值）並 deploy-stg。在此之前本 Task 阻塞。發版次序見 spec §5「發版次序 a→d」。

**Files:** 無（手動 / chrome-devtools / playwright-cli 自動化驗證）。

- [ ] **Step 1: 確認 stg 就緒**

向 `paas-platform #2`（maestri）確認帶放寬的 stg 已起；`API_BASE_URL`（`const.py`）指向該 stg。

- [ ] **Step 2: 跑 HA 開發實例**

Run: `hass -c config`（在 devcontainer）。瀏覽器開 `http://localhost:8123`，HA 登入 `eugene/12341234`。

- [ ] **Step 3: 走設定流程到同意頁**

`http://localhost:8123/config/integrations/dashboard` → 新增 "Woow PaaS Smart Home" → 登入 paas（`admin/admin`）→ 同意頁按「允許」。

- [ ] **Step 4: 驗證 callback（核心驗收）**

確認瀏覽器落在 `…/auth/external/woow/callback`（**非 my.home-assistant.io**），顯示方向 A 成功卡片（「已成功連結 Home Assistant」），`window.close()` 後回到 HA dialog；dialog 自動前進 → 選 `test1` workspace → 選一個 security access → entry 建立成功。

- [ ] **Step 5: 驗證失敗變體（可選）**

竄改 `state` 或在同意頁拒絕 → 確認方向 A 失敗卡片（「Home Assistant 連結失敗」、次鈕「關閉視窗」）。

- [ ] **Step 6: 記錄結果**

把 E2E 結果（成功/失敗截圖、entry 建立與否）回報，並更新 spec §5「發版前待結問題」狀態。

---

## 完成後

- 本 plan 僅涵蓋 **component 側**；paas 側（`check_redirect_uri` 放寬 + seed + healer + docs）由 `paas-platform` agent 依其自有 spec 實作，經 maestri 協調，契約見 spec §5。
- 合併前確認：`python -m pytest tests/ -v` 全綠、hassfest 無錯、E2E（Task 5）通過。
