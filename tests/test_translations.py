"""Config-flow text: every message the flow can show has a translation.

2026-09-30 正式站實測（woowtech-ha，元件 0.3.0）發現兩個直接露給使用者的缺口：
* 建立完成的訊息 "Successfully connected to {name}" 沒拿到 ``name``，HA 前端
  formatjs 把整段換成 ``MISSING_VALUE`` 錯誤；
* 在 PaaS 同意頁按「拒絕」後，HA 對話框只顯示原始代碼 ``user_rejected_authorize``
  ——HA 內建 OAuth2 流程的中止原因，自訂整合要自己在翻譯檔寫文字。
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow

from custom_components.woow_paas_smart_home import config_flow as cf
from custom_components.woow_paas_smart_home.config_flow import ConfigFlow
from custom_components.woow_paas_smart_home.const import PRODUCT_SMART_HOME

_ROOT = Path(__file__).resolve().parents[1]
_STRINGS = json.loads((_ROOT / "strings.json").read_text(encoding="utf-8"))
_TRANSLATIONS = sorted((_ROOT / "translations").glob("*.json"))
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_REASON = re.compile(r'reason="(\w+)"')


def _flatten(node: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


_STRINGS_FLAT = _flatten(_STRINGS)


def _abort_reasons_the_flow_can_raise() -> set[str]:
    """HA 內建 OAuth2 handler 與本元件 config flow 會用到的所有 abort reason。

    從原始碼抓而不是寫死清單：HA 升版新增 reason 時這個測試會自己跟上。
    """
    reasons = set(_REASON.findall(inspect.getsource(config_entry_oauth2_flow)))
    reasons |= set(_REASON.findall(inspect.getsource(cf)))
    reasons |= {
        getattr(cf, name)
        for name in re.findall(r"async_abort\(reason=(ERR_\w+)\)", inspect.getsource(cf))
    }
    # _abort_if_unique_id_configured / flow manager 內建
    reasons |= {"already_configured", "already_in_progress"}
    return reasons


def test_translations_include_zh_hant() -> None:
    assert "zh-Hant.json" in {p.name for p in _TRANSLATIONS}


def test_en_translation_matches_strings() -> None:
    en = json.loads((_ROOT / "translations" / "en.json").read_text(encoding="utf-8"))
    assert en == _STRINGS


@pytest.mark.parametrize("path", _TRANSLATIONS, ids=lambda p: p.name)
def test_translation_has_same_keys_and_placeholders(path: Path) -> None:
    flat = _flatten(json.loads(path.read_text(encoding="utf-8")))
    assert set(flat) == set(_STRINGS_FLAT)
    for key, text in flat.items():
        assert set(_PLACEHOLDER.findall(text)) == set(
            _PLACEHOLDER.findall(_STRINGS_FLAT[key])
        ), key


def test_every_abort_reason_has_text() -> None:
    missing = _abort_reasons_the_flow_can_raise() - set(_STRINGS["config"]["abort"])
    assert not missing, f"abort reasons without translation: {sorted(missing)}"


def test_rejected_authorize_keeps_ha_error_placeholder() -> None:
    # HA 以 description_placeholders={"error": ...} 呼叫這個 abort。
    assert "{error}" in _STRINGS["config"]["abort"]["user_rejected_authorize"]


async def test_create_entry_passes_name_placeholder(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow._oauth_data = {"auth_implementation": "woow_paas_smart_home", "token": {}}
    flow._selected_workspace_id = 3
    flow._selected_workspace_name = "HA Connect Test 0923"
    monkeypatch.setattr(flow, "async_set_unique_id", AsyncMock())
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", MagicMock())
    monkeypatch.setattr(
        flow,
        "_async_api_request",
        AsyncMock(
            return_value={
                "tunnel_token": "tok",
                "tunnel_id": "tid",
                "subdomain": "paas-sm-x.woowtech.io",
            }
        ),
    )

    result = await flow._async_create_entry(
        product_type=PRODUCT_SMART_HOME, instance_id=6, instance_name="sh-e2e-oauth"
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "sh-e2e-oauth"
    assert result["description_placeholders"] == {"name": "sh-e2e-oauth"}
