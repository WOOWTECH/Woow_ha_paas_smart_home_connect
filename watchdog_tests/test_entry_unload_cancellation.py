"""A remote reload must finish cleanup after its own tunnel drops the request."""
import asyncio
import logging
import types
from unittest.mock import AsyncMock, Mock

import pytest

from test_cloudflared_watchdog import SOURCE, manager_factory, wait_until


def unload_function():
    source = SOURCE.with_name('__init__.py').read_text()
    start = source.index('async def async_unload_entry(')
    end = source.index('\n\nasync def async_remove_entry(', start)
    namespace = {'asyncio': asyncio, 'PLATFORMS': ['sensor'],
                 '_LOGGER': logging.getLogger('unload_under_test')}
    exec('from __future__ import annotations\n' + source[start:end], namespace)
    return namespace['async_unload_entry']


@pytest.mark.parametrize('stopping', [False, True])
@pytest.mark.parametrize('cancel_twice', [False, True])
def test_cancelled_request_finishes_owned_child_cleanup(
    manager_factory, stopping, cancel_twice,
):
    _, manager, bus, read, _ = manager_factory
    gate = manager._install_dir / 'term_gate'
    gate.touch()
    hass = manager.hass
    hass.async_create_task = lambda coro, name: asyncio.create_task(coro, name=name)
    hass.config_entries = types.SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True))
    unsubscribe = Mock()
    entry = types.SimpleNamespace(entry_id='synthetic-entry', runtime_data=types.SimpleNamespace(
        coordinator=types.SimpleNamespace(async_cancel_access_reset=AsyncMock()),
        tunnel_manager=manager, unsub_tunnel_status=unsubscribe))

    async def scenario():
        task = None
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            task = asyncio.create_task(unload_function()(hass, entry))
            await wait_until(lambda: (manager._install_dir / 'term_received').exists())
            hass.is_stopping = stopping
            task.cancel('reload HTTP connection closed')
            await asyncio.sleep(.04)
            assert not task.done(), 'Request cancellation abandoned the still-running child'
            if cancel_twice:
                task.cancel('second disconnect cancellation')
                await asyncio.sleep(.04)
                assert not task.done()
            hass.config_entries.async_unload_platforms.assert_not_awaited()
            gate.unlink()
            if stopping:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            else:
                assert await asyncio.wait_for(task, 3) is True
            assert first.returncode is not None
            assert not manager.is_running and not bus.listeners
            assert len(read()) == 1
            hass.config_entries.async_unload_platforms.assert_awaited_once_with(entry, ['sensor'])
            unsubscribe.assert_called_once_with()
        finally:
            gate.unlink(missing_ok=True)
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await manager.stop_tunnel()

    asyncio.run(scenario())
