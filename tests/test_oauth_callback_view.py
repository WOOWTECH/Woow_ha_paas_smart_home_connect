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


def test_render_page_auto_return_mode_toggle() -> None:
    """instant/linger 切換注入為 JS 全域 WOOW_INSTANT / WOOW_LINGER。"""
    from custom_components.woow_paas_smart_home.oauth_callback_view import _render_page

    # 預設（const = linger, 3s）：不在載入即關，倒數可見。
    default = _render_page("success")
    assert "var WOOW_INSTANT=false;" in default
    assert "var WOOW_LINGER=3;" in default

    # instant override：載入即 window.close()。
    instant = _render_page("success", instant=True)
    assert "var WOOW_INSTANT=true;" in instant

    # 倒數秒數可調。
    longer = _render_page("success", linger=5)
    assert "var WOOW_LINGER=5;" in longer


def test_edit_pauses_countdown_machinery_present() -> None:
    """編輯網址時暫停倒數（避免自動返回打斷編輯）的機制有被渲染。"""
    from custom_components.woow_paas_smart_home.oauth_callback_view import _render_page

    success = _render_page("success")
    # 倒數控制函式 + 編輯時的暫停提示
    assert "startCountdown" in success and "stopCountdown" in success
    assert "自動返回已暫停" in success
    # 編輯鈕暫停、存檔/取消重新開始
    assert "click',function(){stopCountdown();" in success
    assert "viewRow.style.display='flex';startCountdown();" in success
