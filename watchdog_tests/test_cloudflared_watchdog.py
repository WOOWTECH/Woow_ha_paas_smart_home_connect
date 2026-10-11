"""Real subprocess lifecycle oracle; no HA API, live CF or actual token."""
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('SHC_MANAGER_SOURCE', str(ROOT / 'cloudflared_manager.py')))


class Bus:
    def __init__(self):
        self.listeners = []

    def async_listen_once(self, event, callback):
        self.listeners.append((event, callback))

        def remove():
            if (event, callback) in self.listeners:
                self.listeners.remove((event, callback))
        return remove

    async def shutdown(self):
        listeners, self.listeners = self.listeners, []
        for _, callback in listeners:
            await callback(None)


@pytest.fixture
def manager_factory(monkeypatch, tmp_path):
    # Only HA wiring is stubbed. Spawn, termination, wait, locks, clocks and
    # cancellation are real OS/asyncio operations throughout every test.
    for name in ['homeassistant','homeassistant.core','homeassistant.const',
                 'homeassistant.helpers','homeassistant.helpers.aiohttp_client']:
        monkeypatch.setitem(sys.modules,name,types.ModuleType(name))
    sys.modules['homeassistant.core'].HomeAssistant = object
    sys.modules['homeassistant.const'].EVENT_HOMEASSISTANT_STOP = 'homeassistant_stop'
    sys.modules['homeassistant.helpers.aiohttp_client'].async_get_clientsession = lambda _: None
    spec = importlib.util.spec_from_file_location('manager_under_test', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module,'_RESTART_RETRY_INITIAL',0.03,raising=False)
    monkeypatch.setattr(module,'_RESTART_RETRY_MAX',0.05,raising=False)
    records = tmp_path/'spawn.jsonl'
    executable = tmp_path/'cloudflared'
    fail_range = tmp_path/'fail_range.json'
    executable.write_text("#!/usr/bin/env python3\n"+f'''
import hashlib,json,os,pathlib,sys,time
records=pathlib.Path({str(records)!r})
count=len(records.read_text().splitlines())+1 if records.exists() else 1
token=sys.argv[sys.argv.index('--token')+1]
with records.open('a') as stream:
 stream.write(json.dumps({{'pid':os.getpid(),'identity':hashlib.sha256(token.encode()).hexdigest(),'count':count}})+'\\n')
fail=pathlib.Path({str(fail_range)!r})
if fail.exists() and count in json.loads(fail.read_text()):
 raise SystemExit(42)
while True: time.sleep(0.03)
''')
    executable.chmod(0o700)
    bus = Bus()
    hass = types.SimpleNamespace(bus=bus,is_stopping=False)
    manager = module.CloudflaredManager(hass,install_dir=str(tmp_path))

    def read():
        return [json.loads(line) for line in records.read_text().splitlines()] if records.exists() else []
    return module, manager, bus, read, fail_range


async def wait_until(predicate, timeout=8):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def test_unexpected_exit_restarts_same_identity(manager_factory):
    _, manager, _, read, _ = manager_factory
    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            first.kill()
            await first.wait()
            await wait_until(lambda: len(read())==2 and manager.is_running)
            assert manager._process.pid != first.pid
            assert read()[0]['identity'] == read()[1]['identity']
            await asyncio.sleep(0.15)
            assert len(read()) == 2
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_manual_stop_remains_stopped_and_can_resume(manager_factory):
    _, manager, bus, read, _ = manager_factory
    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            await manager.stop_tunnel()
            await asyncio.sleep(0.15)
            assert len(read()) == 1 and not manager.is_running
            assert not bus.listeners
            assert await manager.start_tunnel('synthetic-watchdog-token')
            assert len(read()) == 2
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_stop_during_backoff_prevents_resurrection(manager_factory,monkeypatch):
    module, manager, _, read, _ = manager_factory
    monkeypatch.setattr(module,'_RESTART_RETRY_INITIAL',0.3,raising=False)
    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first=manager._process
            first.kill()
            await first.wait()
            await asyncio.sleep(0.05)
            await manager.stop_tunnel()
            await asyncio.sleep(0.4)
            assert len(read())==1 and not manager.is_running
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_concurrent_starts_have_one_process_and_one_restart(manager_factory):
    _, manager, _, read, _ = manager_factory
    async def scenario():
        try:
            assert all(await asyncio.gather(*(manager.start_tunnel('synthetic-watchdog-token') for _ in range(10))))
            assert len(read())==1
            first=manager._process
            first.kill()
            await first.wait()
            await wait_until(lambda: len(read())==2 and manager.is_running)
            await asyncio.sleep(0.15)
            assert len(read())==2
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_failed_restart_retries_and_caps_backoff(manager_factory,caplog):
    _, manager, _, read, fail_range = manager_factory
    fail_range.write_text(json.dumps([2,3,4]))
    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first=manager._process
            first.kill()
            await first.wait()
            await wait_until(lambda: len(read())==5 and manager.is_running)
            assert len({row['identity'] for row in read()})==1
            assert '0.05 seconds' in caplog.text
        finally:
            await manager.stop_tunnel()
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())


def test_ha_shutdown_stops_process_and_retries(manager_factory):
    _, manager, bus, read, _ = manager_factory
    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            assert len(bus.listeners)==1, 'process must have an HA shutdown hook'
            process=manager._process
            manager.hass.is_stopping=True
            await bus.shutdown()
            await asyncio.sleep(0.15)
            assert process.returncode is not None and not manager.is_running
            assert len(read())==1 and not bus.listeners
            assert not await manager.start_tunnel('synthetic-watchdog-token')
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_cancelled_startup_cleans_the_spawned_child(manager_factory):
    _, manager, bus, read, _ = manager_factory
    async def scenario():
        task=asyncio.create_task(manager.start_tunnel('synthetic-watchdog-token'))
        try:
            await wait_until(lambda: len(read())==1 and manager.is_running)
            process=manager._process
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.15)
            assert process.returncode is not None and not manager.is_running
            assert len(read())==1 and not bus.listeners
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            await manager.stop_tunnel()
    asyncio.run(scenario())
