"""Real subprocess lifecycle oracle; no HA API, live CF or actual token."""
import asyncio
import importlib.util
import json
import logging
import os
import signal
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
import hashlib,json,os,pathlib,signal,sys,time
records=pathlib.Path({str(records)!r})
count=len(records.read_text().splitlines())+1 if records.exists() else 1
token=sys.argv[sys.argv.index('--token')+1]
with records.open('a') as stream:
 stream.write(json.dumps({{'pid':os.getpid(),'identity':hashlib.sha256(token.encode()).hexdigest(),'count':count}})+'\\n')
fail=pathlib.Path({str(fail_range)!r})
if fail.exists() and count in json.loads(fail.read_text()):
 raise SystemExit(42)
if pathlib.Path({str(tmp_path/'ignore_term')!r}).exists():
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
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


@pytest.mark.parametrize('initial_failure', ['immediate_exit', 'spawn_oserror'])
def test_entry_retries_initial_failed_spawn_then_has_one_watcher(
    manager_factory, monkeypatch, initial_failure,
):
    module, manager, bus, read, fail_range = manager_factory
    recovered_count = 2 if initial_failure == 'immediate_exit' else 1
    failed_spawns = []
    if initial_failure == 'immediate_exit':
        fail_range.write_text(json.dumps([1]))
    else:
        manager._binary_path.chmod(0o600)
        real_spawn = asyncio.create_subprocess_exec

        async def restore_permission_after_real_spawn_error(*args, **kwargs):
            try:
                return await real_spawn(*args, **kwargs)
            except PermissionError as err:
                # Exercise a genuine OS exec failure, then restore the external
                # condition before the outer entry loop retries.
                failed_spawns.append(err.errno)
                manager._binary_path.chmod(0o700)
                raise

        monkeypatch.setattr(module.asyncio, 'create_subprocess_exec',
                            restore_permission_after_real_spawn_error)
    # Execute the exact entry bring-up function with HA wiring isolated. The
    # rest of __init__.py requires HA and Python 3.13; do not rewrite this body.
    entry_source = SOURCE.with_name('__init__.py').read_text()
    start = entry_source.index('async def _async_start_tunnel_with_retry(')
    end = entry_source.index('\n\nasync def async_setup_entry(', start)
    namespace = {
        'asyncio': asyncio,
        '_LOGGER': logging.getLogger('entry_under_test'),
        '_TUNNEL_RETRY_INITIAL': 0.03,
        '_TUNNEL_RETRY_MAX': 0.05,
        'CONF_TUNNEL_TOKEN': 'tunnel_token',
    }
    exec('from __future__ import annotations\n' + entry_source[start:end], namespace)
    entry = types.SimpleNamespace(data={'tunnel_token': 'synthetic-watchdog-token'})

    async def scenario():
        try:
            await asyncio.wait_for(
                namespace['_async_start_tunnel_with_retry'](
                    entry, manager, 'smart_home', 'synthetic-instance'
                ),
                timeout=8,
            )
            assert len(read()) == recovered_count and manager.is_running
            if initial_failure == 'spawn_oserror':
                assert failed_spawns == [13], 'the first exec must really fail in the OS'
            assert len(bus.listeners) == 1
            watchdog = manager._watchdog_task
            assert watchdog is not None and not watchdog.done()
            first = manager._process
            first.kill()
            await first.wait()
            await wait_until(lambda: len(read()) == recovered_count + 1 and manager.is_running)
            assert all(await asyncio.gather(*(
                manager.start_tunnel('synthetic-watchdog-token') for _ in range(10)
            )))
            await asyncio.sleep(0.15)
            assert len(read()) == recovered_count + 1
            assert len({row['identity'] for row in read()}) == 1
            assert manager._watchdog_task is watchdog
            assert sum(task.get_name() == 'woow_cloudflared_watchdog'
                       for task in asyncio.all_tasks()) == 1
            assert await manager.stop_tunnel()
            await asyncio.sleep(0.15)
            assert not manager.is_running and len(read()) == recovered_count + 1
            assert not bus.listeners
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != 'linux', reason='inherited pipe oracle uses /proc')
@pytest.mark.parametrize('defer_sigkill', [False, True])
def test_stop_cleanup_matches_native_wait_outcome(manager_factory, monkeypatch, defer_sigkill):
    _, manager, bus, read, _ = manager_factory
    (manager._install_dir / 'ignore_term').touch()

    async def scenario():
        process = helper = None
        kill_dispatch = None
        real_kill = None
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            process = manager._process
            real_kill = process.kill
            native_wait = process.wait
            watchdog_task = manager._watchdog_task
            wait_entry_codes = []
            wait_completed = []

            async def observed_wait():
                # Observe only stop's waits, delegating to the unchanged native
                # method. The concurrently starting watchdog also waits here.
                if asyncio.current_task() is not watchdog_task:
                    wait_entry_codes.append(process.returncode)
                    slot = len(wait_completed)
                    wait_completed.append(False)
                    result = await native_wait()
                    wait_completed[slot] = True
                    return result
                return await native_wait()

            monkeypatch.setattr(process, 'wait', observed_wait)
            if defer_sigkill:
                # Register the real wait before dispatching the real SIGKILL.
                # Otherwise CPython may observe exit first and legitimately
                # return immediately despite an inherited pipe remaining open.
                def dispatch_kill():
                    nonlocal kill_dispatch
                    kill_dispatch = asyncio.get_running_loop().call_later(0.05, real_kill)
                monkeypatch.setattr(process, 'kill', dispatch_kill)
            # A directly owned real helper retains the managed child's stdout
            # pipe after SIGKILL. Both children are reaped by this test; no
            # orphan grandchild, fake Process/wait result or timeout injection.
            writer = os.open(f'/proc/{process.pid}/fd/1', os.O_WRONLY)
            try:
                helper = await asyncio.create_subprocess_exec(
                    sys.executable, '-c', 'import time; time.sleep(60)',
                    stdout=writer, stderr=asyncio.subprocess.DEVNULL,
                )
            finally:
                os.close(writer)
            stopped = await manager.stop_tunnel()
            assert len(wait_entry_codes) == 2
            # 判準是 native wait 實際有沒有在 stop 的期限內返回，不是進入時的
            # returncode：CPython 3.13.15 起子程序結束就讓 wait() 返回，不再等被
            # 繼承的 pipe 關閉（3.13.12 仍會等）。兩種行為 stop 都必須如實回報。
            pending_wait = not wait_completed[1]
            if defer_sigkill:
                assert wait_entry_codes[1] is None, (
                    'controlled SIGKILL must register the real wait before exit')
            assert stopped is (not pending_wait), (
                'cleanup must match native wait outcome: '
                f'stop={stopped}, handle_retained={manager._process is process}, '
                f'child_returncode={process.returncode}, '
                f'helper_running={helper.returncode is None}, wait_entry_codes={wait_entry_codes}'
            )
            assert manager._process is (process if pending_wait else None)
            assert process.returncode == -signal.SIGKILL
            assert helper.returncode is None
            assert manager._desired_token is None
            assert manager._watchdog_task is None
            assert not bus.listeners
            if pending_wait:
                assert manager._stdout_task is not None
            else:
                assert manager._stdout_task is None and manager._stderr_task is None
            await asyncio.sleep(0.15)
            assert len(read()) == 1, 'explicit stop must not restart during failed cleanup'
            helper.terminate()
            await helper.wait()
            if pending_wait:
                await asyncio.gather(manager._stdout_task, manager._stderr_task)
            assert await manager.stop_tunnel()
            assert manager._process is None and not manager.is_running
            assert manager._stdout_task is None and manager._stderr_task is None
            assert len(read()) == 1
        finally:
            if kill_dispatch is not None:
                kill_dispatch.cancel()
            if helper is not None and helper.returncode is None:
                helper.kill()
                await helper.wait()
            if process is not None:
                if process.returncode is None:
                    real_kill()
                await process.wait()
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_exit_notifies_local_status_before_production_backoff(manager_factory, monkeypatch):
    """A dead real child must publish stopped state before the unchanged 30s retry."""
    module, manager, _, read, _ = manager_factory
    monkeypatch.setattr(module, '_RESTART_RETRY_INITIAL', 30)
    changes = []
    # Set the observer slot directly to drive the exit path on the old code too;
    # separate tests cover public registration and native setup wiring.
    manager._state_change_callback = lambda: changes.append(manager.is_running)

    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            changes.clear()
            process = manager._process
            process.kill()
            await process.wait()
            await wait_until(lambda: changes == [False], timeout=0.5)
            assert not manager.is_running and len(read()) == 1
            assert manager._watchdog_task is not None
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_successful_watchdog_spawn_notifies_running_without_poll(manager_factory):
    """Both exit and successful replacement publish real local state."""
    _, manager, _, read, _ = manager_factory
    changes = []
    manager._state_change_callback = lambda: changes.append(manager.is_running)

    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            changes.clear()
            process = manager._process
            process.kill()
            await process.wait()
            await wait_until(lambda: changes == [False, True], timeout=4)
            assert manager.is_running and len(read()) == 2
            assert manager._process.pid != process.pid
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_public_listener_unsubscribe_preserves_stop_and_restart(manager_factory):
    """Detached observers stay detached; old cleanup cannot remove a new observer."""
    _, manager, _, read, _ = manager_factory
    old_changes, changes = [], []
    remove_old = manager.async_add_state_listener(lambda: old_changes.append(manager.is_running))
    remove = manager.async_add_state_listener(lambda: changes.append(manager.is_running))
    remove_old()

    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            assert changes == [True] and old_changes == []
            remove()
            remove()  # native cleanup can be idempotent
            process = manager._process
            process.kill()
            await process.wait()
            await wait_until(lambda: len(read()) == 2 and manager.is_running)
            # is_running becomes true as soon as spawn returns, before the
            # existing 1s startup check notifies; wait for that check to finish.
            await asyncio.sleep(1.1)
            assert changes == [True] and old_changes == []
            assert await manager.stop_tunnel()
            await asyncio.sleep(0.15)
            assert len(read()) == 2 and not manager.is_running
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_listener_failure_does_not_break_watchdog_or_stop(manager_factory):
    """A UI observer cannot abandon the real managed child or its watchdog."""
    _, manager, _, read, _ = manager_factory
    def listener():
        raise RuntimeError('synthetic observer failure')
    remove = manager.async_add_state_listener(listener)

    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            watchdog = manager._watchdog_task
            process = manager._process
            process.kill()
            await process.wait()
            await wait_until(lambda: len(read()) == 2 and manager.is_running)
            await asyncio.sleep(1.1)
            assert manager._watchdog_task is watchdog and not watchdog.done()
        finally:
            remove()
            assert await manager.stop_tunnel()
    asyncio.run(scenario())
