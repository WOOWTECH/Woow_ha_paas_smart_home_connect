"""Unit tests for WoowOAuth2CallbackView.

Builds a mocked aiohttp request (query + app[KEY_HASS]) and drives get()
directly. A valid signed `state` is produced with HA Core's _encode_jwt so the
view's _decode_jwt reuse is exercised end to end. The flow is spied via
monkeypatched async_configure. `hass` comes from HA core's tests/conftest.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from multidict import MultiDict
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.helpers import http
from homeassistant.helpers.config_entry_oauth2_flow import _encode_jwt

from custom_components.woow_paas_smart_home.const import (
    DATA_CALLBACK_VIEW_REGISTERED,
)
from custom_components.woow_paas_smart_home.oauth_callback_view import (
    WoowOAuth2CallbackView,
    async_register_woow_callback_view,
)


def _request(
    hass: HomeAssistant, query: dict | list[tuple[str, str]] | MultiDict
) -> MagicMock:
    request = MagicMock()
    request.query = MultiDict(query)
    request.app = {http.KEY_HASS: hass}
    return request


def _spy_flow(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    spy = AsyncMock(return_value={"type": "form", "step_id": "select_workspace"})
    monkeypatch.setattr(hass.config_entries.flow, "async_configure", spy)
    return spy


async def test_code_resumes_flow_without_claiming_completion(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _spy_flow(hass, monkeypatch)
    spy.return_value = {"type": FlowResultType.EXTERNAL_STEP_DONE}
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
    assert "請返回 Home Assistant 確認連結結果" in resp.text
    assert "已成功連結 Home Assistant" not in resp.text
    assert 'data-status="pending"' in resp.text


async def test_aborted_code_exchange_renders_error(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _spy_flow(hass, monkeypatch)
    spy.return_value = {"type": FlowResultType.ABORT}
    state = _encode_jwt(hass, {"flow_id": "flow-1"})

    resp = await WoowOAuth2CallbackView().get(
        _request(hass, {"state": state, "code": "the-code"})
    )

    assert resp.status == 200
    assert 'data-status="error"' in resp.text


async def test_reauth_success_retains_completed_status(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _spy_flow(hass, monkeypatch)
    spy.return_value = {"type": FlowResultType.ABORT, "reason": "reauth_successful"}
    state = _encode_jwt(hass, {"flow_id": "flow-1"})

    resp = await WoowOAuth2CallbackView().get(
        _request(hass, {"state": state, "code": "the-code"})
    )

    assert resp.status == 200
    assert 'data-status="success"' in resp.text


async def test_replayed_callback_returns_400(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _spy_flow(hass, monkeypatch)
    spy.side_effect = UnknownFlow("Flow not found")
    state = _encode_jwt(hass, {"flow_id": "expired"})

    resp = await WoowOAuth2CallbackView().get(
        _request(hass, {"state": state, "code": "replayed"})
    )

    assert resp.status == 400
    assert "Home Assistant 連結失敗" in resp.text


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


@pytest.mark.parametrize(
    "query",
    [
        [("state", "valid"), ("state", "wrong"), ("code", "x")],
        [("state", "valid"), ("code", "x"), ("code", "y")],
        [("state", "valid"), ("code", "x"), ("error", "access_denied")],
        [("state", "valid"), ("error", "one"), ("error", "two")],
    ],
)
async def test_ambiguous_callback_rejected_before_flow(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, query: list[tuple[str, str]]
) -> None:
    spy = _spy_flow(hass, monkeypatch)
    response = await WoowOAuth2CallbackView().get(_request(hass, MultiDict(query)))

    assert response.status == 400
    assert "Home Assistant 連結失敗" in response.text
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


def test_rendered_page_matches_woowtech_brand_and_truthful_status() -> None:
    """官網色彩、MDI 與完成設定前的正確授權狀態。"""
    from custom_components.woow_paas_smart_home.oauth_callback_view import _render_page

    success = _render_page("success")
    pending = _render_page("pending")
    error = _render_page("error")

    assert "#6183fc" in success and "#eff1f5" in success
    assert "mdi-home-assistant" in success
    assert ">woowtech</div>" in success
    assert "繼續選擇工作區與服務" in success
    assert "/config/integrations/dashboard" in success
    # 狀態文案分流
    assert "正在返回整合頁面" in success and "立即返回整合頁" in success
    assert "連結結果以整合頁顯示為準" in pending
    assert 'data-status="pending"' in pending
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


def test_return_is_bound_to_current_ha_origin() -> None:
    """舊瀏覽器儲存的網址不會把授權結果導到另一台 HA。"""
    from custom_components.woow_paas_smart_home.oauth_callback_view import _render_page

    success = _render_page("success")
    assert "var haUrl=location.origin;" in success
    assert "location.replace(haUrl+'/config/integrations/dashboard')" in success
    assert "localStorage" not in success and "woow_ha_url" not in success
    assert "<input" not in success
