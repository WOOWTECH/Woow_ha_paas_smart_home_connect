"""Installer and readiness regressions using real HA imports and synthetic data."""

import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import zipfile

import pytest

from custom_components.woow_paas_smart_home import mcp_setup as setup
from custom_components.woow_paas_smart_home.const import McpIntegrationState
from custom_components.woow_paas_smart_home.coordinator import (
    async_detect_ha_mcp_state, async_get_ha_mcp_webhook_id,
)


def _archive(monkeypatch, extra=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        for name, content in {
            'manifest.json': json.dumps({'domain': 'ha_mcp_tools', 'version': '1.2.2'}),
            '__init__.py': '', 'config_flow.py': '', **(extra or {}),
        }.items():
            z.writestr(setup._ARCHIVE_PREFIX + name, content)
    payload = data.getvalue()
    monkeypatch.setattr(setup, 'MCP_ARCHIVE_SHA256', hashlib.sha256(payload).hexdigest())
    return payload


def _hass(tmp_path, entries=()):
    async def executor(fn, *args):
        return fn(*args)
    flow = MagicMock()
    flow.async_init = AsyncMock(return_value={'type': 'menu', 'menu_options': ['server', 'tools'], 'flow_id': 'synthetic'})
    flow.async_configure = AsyncMock(side_effect=[
        {'type': 'form', 'step_id': 'server', 'flow_id': 'synthetic'},
        {'type': 'create_entry'},
    ])
    flow.async_progress.return_value = []
    return SimpleNamespace(
        data={}, config=SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p))),
        config_entries=SimpleNamespace(async_entries=lambda _: list(entries), flow=flow),
        async_add_executor_job=executor,
    )


@pytest.fixture(autouse=True)
def supported_version(monkeypatch):
    monkeypatch.setattr(setup, 'HA_VERSION', '2026.7.2')


def test_archive_installs_component_only(tmp_path, monkeypatch):
    payload = _archive(monkeypatch)
    target = tmp_path / 'ha_mcp_tools'
    setup._install_archive(payload, target)
    assert json.loads((target / 'manifest.json').read_text())['version'] == '1.2.2'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['ha_mcp_tools']


def test_archive_checksum_failure_leaves_no_installation(tmp_path, monkeypatch):
    payload = _archive(monkeypatch)
    with pytest.raises(setup.McpSetupError):
        setup._install_archive(payload + b'changed', tmp_path / 'ha_mcp_tools')
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('unsafe', ['../escape.py', '/absolute.py', 'dir/../../escape.py', 'dir\\escape.py'])
def test_archive_rejects_unsafe_paths(tmp_path, monkeypatch, unsafe):
    payload = _archive(monkeypatch, {unsafe: 'unsafe'})
    with pytest.raises(setup.McpSetupError):
        setup._install_archive(payload, tmp_path / 'ha_mcp_tools')
    assert not list(tmp_path.iterdir())


def test_existing_component_preserved(tmp_path, monkeypatch):
    payload = _archive(monkeypatch)
    target = tmp_path / 'ha_mcp_tools'
    target.mkdir()
    (target / 'manifest.json').write_text('original')
    setup._install_archive(payload, target)
    assert (target / 'manifest.json').read_text() == 'original'
    assert not (target / 'config_flow.py').exists()


def test_missing_component_downloads_and_creates_server(tmp_path, monkeypatch):
    hass = _hass(tmp_path)
    download = AsyncMock(return_value=_archive(monkeypatch))
    monkeypatch.setattr(setup, '_async_download', download)
    monkeypatch.setattr(setup, 'async_get_custom_components', AsyncMock(side_effect=[{}, {'ha_mcp_tools': object()}]))
    asyncio.run(setup.async_ensure_mcp_server(hass))
    download.assert_awaited_once()
    assert Path(hass.config.path('custom_components', 'ha_mcp_tools', 'manifest.json')).is_file()
    assert hass.config_entries.flow.async_configure.await_args_list[0].args == ('synthetic', {'next_step_id': 'server'})
    assert hass.config_entries.flow.async_configure.await_args_list[1].args == ('synthetic', {})
    assert setup.DATA_INSTALLING not in hass.data


def test_no_subscription_or_old_ha_never_downloads(tmp_path, monkeypatch):
    hass = _hass(tmp_path)
    download = AsyncMock()
    monkeypatch.setattr(setup, '_async_download', download)
    asyncio.run(setup.async_ensure_mcp_server(hass, lambda: False))
    monkeypatch.setattr(setup, 'HA_VERSION', '2026.3.4')
    with pytest.raises(setup.McpSetupError, match='mcp_ha_unsupported'):
        asyncio.run(setup.async_ensure_mcp_server(hass))
    download.assert_not_called()
    hass.config_entries.flow.async_init.assert_not_called()


def test_unsubscribed_during_download_never_installs(tmp_path, monkeypatch):
    hass = _hass(tmp_path)
    eligible = True
    payload = _archive(monkeypatch)
    async def download(_):
        nonlocal eligible
        eligible = False
        return payload
    monkeypatch.setattr(setup, '_async_download', download)
    asyncio.run(setup.async_ensure_mcp_server(hass, lambda: eligible))
    assert not Path(hass.config.path('custom_components', 'ha_mcp_tools')).exists()
    hass.config_entries.flow.async_init.assert_not_called()


@pytest.mark.parametrize('disabled,webhook,error', [
    ('user', True, 'mcp_disabled'), (None, False, 'mcp_webhook_disabled'), (None, True, None),
])
def test_existing_server_keeps_authentication_and_user_choices(tmp_path, monkeypatch, disabled, webhook, error):
    entry = SimpleNamespace(data={'entry_type': 'server', 'webhook_id': 'existing'}, disabled_by=disabled,
                            options={'enable_webhook': webhook, 'webhook_auth': 'ha_auth'})
    hass = _hass(tmp_path, [entry])
    Path(hass.config.path('custom_components', 'ha_mcp_tools')).mkdir(parents=True)
    monkeypatch.setattr(setup, 'async_get_custom_components', AsyncMock(return_value={'ha_mcp_tools': object()}))
    if error:
        with pytest.raises(setup.McpSetupError, match=error):
            asyncio.run(setup.async_ensure_mcp_server(hass))
    else:
        asyncio.run(setup.async_ensure_mcp_server(hass))
    hass.config_entries.flow.async_init.assert_not_called()
    assert entry.options['webhook_auth'] == 'ha_auth'
    assert entry.data['webhook_id'] == 'existing'


def test_tools_only_creates_native_server(tmp_path, monkeypatch):
    hass = _hass(tmp_path, [SimpleNamespace(data={'entry_type': 'tools'})])
    Path(hass.config.path('custom_components', 'ha_mcp_tools')).mkdir(parents=True)
    monkeypatch.setattr(setup, 'async_get_custom_components', AsyncMock(return_value={'ha_mcp_tools': object()}))
    asyncio.run(setup.async_ensure_mcp_server(hass))
    assert hass.config_entries.flow.async_configure.await_count == 2


@pytest.mark.parametrize('bringing_up,running,should_reload', [
    (False, False, True), (True, False, False), (False, True, False),
])
def test_failed_background_start_retries_without_restarting_healthy_server(
    tmp_path, monkeypatch, bringing_up, running, should_reload,
):
    entry = SimpleNamespace(data={'entry_type': 'server'}, options={}, disabled_by=None,
                            entry_id='existing', state=setup.ConfigEntryState.LOADED)
    hass = _hass(tmp_path, [entry])
    hass.config_entries.async_reload = AsyncMock(return_value=True)
    hass.data['ha_mcp_tools'] = {
        'manager': SimpleNamespace(is_running=running),
        'bringup_task': SimpleNamespace(done=lambda: not bringing_up),
        'webhook': {'ready': True} if running else {},
    }
    Path(hass.config.path('custom_components', 'ha_mcp_tools')).mkdir(parents=True)
    monkeypatch.setattr(setup, 'async_get_custom_components', AsyncMock(return_value={'ha_mcp_tools': object()}))
    asyncio.run(setup.async_ensure_mcp_server(hass))
    assert hass.config_entries.async_reload.await_count == int(should_reload)


def test_concurrent_setups_create_only_one_server(tmp_path, monkeypatch):
    entries = []
    hass = _hass(tmp_path, entries)
    Path(hass.config.path('custom_components', 'ha_mcp_tools')).mkdir(parents=True)
    monkeypatch.setattr(setup, 'async_get_custom_components', AsyncMock(return_value={'ha_mcp_tools': object()}))
    async def configure(flow_id, data):
        await asyncio.sleep(0)
        if 'next_step_id' in data:
            return {'type': 'form', 'step_id': 'server', 'flow_id': flow_id}
        entries.append(SimpleNamespace(data={'entry_type': 'server'}, disabled_by=None, options={}))
        return {'type': 'create_entry'}
    hass.config_entries.flow.async_configure = AsyncMock(side_effect=configure)
    async def run():
        await asyncio.gather(setup.async_ensure_mcp_server(hass), setup.async_ensure_mcp_server(hass))
    asyncio.run(run())
    hass.config_entries.flow.async_init.assert_awaited_once()
    assert len(entries) == 1


def _running(entry_type='server', running=True, registered=True, local_only=False):
    entry = SimpleNamespace(data={'entry_type': entry_type, 'webhook_id': 'mcp_synthetic'}, options={})
    return SimpleNamespace(
        data={'ha_mcp_tools': {'manager': SimpleNamespace(is_running=running), 'webhook': {'ready': True}},
              'webhook': {'mcp_synthetic': SimpleNamespace(domain='ha_mcp_tools', local_only=local_only,
                           allowed_methods={'POST', 'GET'})} if registered else {}},
        config_entries=SimpleNamespace(async_loaded_entries=lambda _: [entry]),
        config=SimpleNamespace(path=lambda *args: '/synthetic/manifest.json'),
        async_add_executor_job=AsyncMock(return_value=True),
    )


@pytest.mark.parametrize('kwargs', [{'entry_type': 'tools'}, {'running': False}, {'registered': False}, {'local_only': True}])
def test_unready_or_local_endpoint_has_no_url(kwargs):
    assert async_get_ha_mcp_webhook_id(_running(**kwargs)) is None


def test_tools_entry_is_not_running_server():
    assert asyncio.run(async_detect_ha_mcp_state(_running(entry_type='tools'))) == McpIntegrationState.INSTALLED_NOT_RUNNING


def test_ready_server_returns_effective_webhook():
    hass = _running()
    assert async_get_ha_mcp_webhook_id(hass) == 'mcp_synthetic'
    assert asyncio.run(async_detect_ha_mcp_state(hass)) == McpIntegrationState.RUNNING


def test_live_thread_before_readiness_is_not_running():
    hass = _running()
    hass.data['ha_mcp_tools'].pop('webhook')
    assert asyncio.run(async_detect_ha_mcp_state(hass)) == McpIntegrationState.INSTALLED_NOT_RUNNING
    assert async_get_ha_mcp_webhook_id(hass) is None


def test_starting_and_installing_states():
    hass = _running(running=False)
    hass.data[setup.DATA_INSTALLING] = True
    assert asyncio.run(async_detect_ha_mcp_state(hass)) == McpIntegrationState.INSTALLING
    hass.data.pop(setup.DATA_INSTALLING)
    hass.data['ha_mcp_tools']['bringup_task'] = SimpleNamespace(done=lambda: False)
    assert asyncio.run(async_detect_ha_mcp_state(hass)) == McpIntegrationState.STARTING
