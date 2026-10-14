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
gate=pathlib.Path({str(tmp_path/'term_gate')!r})
def gated_term(*_):
 pathlib.Path({str(tmp_path/'term_received')!r}).touch()
 while gate.exists(): time.sleep(0.01)
 raise SystemExit(0)
if gate.exists(): signal.signal(signal.SIGTERM,gated_term)
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

            async def observed_wait():
                # Observe only stop's waits, delegating to the unchanged native
                # method. The concurrently starting watchdog also waits here.
                if asyncio.current_task() is not watchdog_task:
                    wait_entry_codes.append(process.returncode)
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
            pending_wait = wait_entry_codes[1] is None
            if defer_sigkill:
                assert pending_wait, 'controlled SIGKILL must exercise the real pending wait'
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


def test_reset_verifies_exit_then_restarts_same_identity(manager_factory):
    _, manager, bus, read, _ = manager_factory

    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            receipt = await manager.reset_tunnel()
            assert receipt.cutoff_verified is True and receipt.restarted is True
            assert first.returncode is not None
            assert manager.is_running and manager._process.pid != first.pid
            assert len(read()) == 2
            assert read()[0]['identity'] == read()[1]['identity']
            assert len(bus.listeners) == 1
            with pytest.raises(AttributeError):
                receipt.cutoff_verified = False
            assert 'synthetic-watchdog-token' not in repr(receipt)
            replacement = manager._process
            replacement.kill()
            await replacement.wait()
            await wait_until(lambda: len(read()) == 3 and manager.is_running)
            assert manager._process.pid != replacement.pid
            assert len({row['identity'] for row in read()}) == 1
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_reset_cancelled_replacement_is_stopped_without_recovery(manager_factory):
    _, manager, bus, read, _ = manager_factory

    async def scenario():
        task = None
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            task = asyncio.create_task(manager.reset_tunnel())
            await wait_until(lambda: len(read()) == 2 and manager.is_running)
            replacement = manager._process
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.15)
            assert first.returncode is not None and replacement.returncode is not None
            assert not manager.is_running and len(read()) == 2
            assert manager._desired_token is None and not bus.listeners
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_reset_shutdown_during_replacement_reports_no_restart(manager_factory):
    _, manager, bus, read, _ = manager_factory

    async def scenario():
        reset = shutdown = None
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            reset = asyncio.create_task(manager.reset_tunnel())
            await wait_until(lambda: len(read()) == 2 and manager.is_running)
            replacement = manager._process
            manager.hass.is_stopping = True
            shutdown = asyncio.create_task(bus.shutdown())
            receipt = await reset
            await shutdown
            assert receipt.cutoff_verified is True and receipt.restarted is False
            assert first.returncode is not None and replacement.returncode is not None
            assert len(read()) == 2 and not manager.is_running and not bus.listeners
        finally:
            for task in (reset, shutdown):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await manager.stop_tunnel()
    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != 'linux', reason='inherited pipe oracle uses /proc')
@pytest.mark.parametrize('kill_delay', [0.05, 60])
@pytest.mark.parametrize('cancel_reset', [False, True])
def test_reset_timeout_retains_owner_and_blocks_manual_replacement(manager_factory, monkeypatch, kill_delay, cancel_reset):
    _, manager, bus, read, _ = manager_factory
    (manager._install_dir / 'ignore_term').touch()

    async def scenario():
        process = helper = dispatch = reset = None
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            process = manager._process
            real_kill = process.kill
            # Real SIGKILL after native wait registration; owned helper keeps
            # stdout open so the final native wait actually times out.
            def delayed_kill():
                nonlocal dispatch
                dispatch = asyncio.get_running_loop().call_later(kill_delay, real_kill)
            monkeypatch.setattr(process, 'kill', delayed_kill)
            writer = os.open(f'/proc/{process.pid}/fd/1', os.O_WRONLY)
            try:
                helper = await asyncio.create_subprocess_exec(
                    sys.executable, '-c', 'import time; time.sleep(60)',
                    stdout=writer, stderr=asyncio.subprocess.DEVNULL,
                )
            finally:
                os.close(writer)
            term_sent = asyncio.Event()
            real_signal = process.send_signal
            def observed_signal(sig):
                real_signal(sig)
                if sig == signal.SIGTERM:
                    term_sent.set()
            monkeypatch.setattr(process, 'send_signal', observed_signal)
            reset = asyncio.create_task(manager.reset_tunnel())
            if cancel_reset:
                await term_sent.wait()
                reset.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await reset
            else:
                receipt = await reset
                assert receipt.cutoff_verified is False and receipt.restarted is False
            assert manager._process is process
            assert process.returncode == (-signal.SIGKILL if kill_delay < 1 else None)
            assert len(read()) == 1 and not bus.listeners
            assert not await manager.start_tunnel('synthetic-watchdog-token')
            await asyncio.sleep(0.15)
            assert len(read()) == 1 and manager._process is process
            assert manager._desired_token is None
            helper.terminate()
            await helper.wait()
            if process.returncode is None:
                real_kill()
                await process.wait()
            assert await manager.stop_tunnel()
            (manager._install_dir / 'ignore_term').unlink()
            assert await manager.start_tunnel('synthetic-watchdog-token')
            assert len(read()) == 2
        finally:
            if reset is not None and not reset.done():
                reset.cancel()
                await asyncio.gather(reset, return_exceptions=True)
            if dispatch is not None:
                dispatch.cancel()
            if helper is not None and helper.returncode is None:
                helper.kill()
                await helper.wait()
            if process is not None:
                if process.returncode is None:
                    real_kill()
                await process.wait()
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_reset_cancelled_while_spawn_returns_keeps_child_owned(manager_factory, monkeypatch):
    module, manager, bus, read, _ = manager_factory
    real_spawn = asyncio.create_subprocess_exec

    async def scenario():
        reset = None
        spawned = []
        release = asyncio.Event()
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')

            async def observed_spawn(*args, **kwargs):
                process = await real_spawn(*args, **kwargs)
                spawned.append(process)
                await release.wait()
                return process

            monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', observed_spawn)
            reset = asyncio.create_task(manager.reset_tunnel())
            await wait_until(lambda: len(spawned) == 1)
            reset.cancel()
            asyncio.get_running_loop().call_later(0.05, release.set)
            with pytest.raises(asyncio.CancelledError):
                await reset
            assert spawned[0].returncode is not None
            assert not manager.is_running and manager._desired_token is None
            assert not bus.listeners and len(read()) == 2
        finally:
            release.set()
            if reset is not None and not reset.done():
                reset.cancel()
                await asyncio.gather(reset, return_exceptions=True)
            for process in spawned:
                if process.returncode is None:
                    process.kill()
                await process.wait()
            await manager.stop_tunnel()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['immediate_exit', 'spawn_oserror'])
def test_reset_restart_failure_keeps_cutoff_without_recovery(manager_factory, failure):
    _, manager, bus, read, fail_range = manager_factory

    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            if failure == 'immediate_exit':
                fail_range.write_text(json.dumps([2]))
            else:
                manager._binary_path.chmod(0o600)
            receipt = await manager.reset_tunnel()
            assert receipt.cutoff_verified is True and receipt.restarted is False
            assert first.returncode is not None and not manager.is_running
            await asyncio.sleep(0.15)
            assert len(read()) == (2 if failure == 'immediate_exit' else 1)
            assert manager._desired_token is None and not bus.listeners
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_reset_escalates_term_to_kill_before_replacement(manager_factory):
    _, manager, _, read, _ = manager_factory

    async def scenario():
        try:
            (manager._install_dir / 'ignore_term').touch()
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            (manager._install_dir / 'ignore_term').unlink()
            receipt = await manager.reset_tunnel()
            assert receipt.cutoff_verified is True and receipt.restarted is True
            assert first.returncode == -signal.SIGKILL and len(read()) == 2
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


@pytest.mark.parametrize('queued_operation', ['start', 'stop', 'reset'])
def test_reset_serializes_manual_lifecycle_calls(manager_factory, queued_operation):
    _, manager, _, read, _ = manager_factory
    gate = manager._install_dir / 'term_gate'
    gate.touch()

    async def scenario():
        reset = queued = None
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            reset = asyncio.create_task(manager.reset_tunnel())
            await wait_until(lambda: (manager._install_dir / 'term_received').exists())
            operation = {
                'start': lambda: manager.start_tunnel('synthetic-watchdog-token'),
                'stop': manager.stop_tunnel,
                'reset': manager.reset_tunnel,
            }[queued_operation]
            queued = asyncio.create_task(operation())
            await asyncio.sleep(0.05)
            assert not queued.done() and len(read()) == 1
            gate.unlink()
            receipt = await reset
            assert receipt.cutoff_verified is True and receipt.restarted is True
            assert first.returncode is not None
            result = await queued
            if queued_operation == 'reset':
                assert result.cutoff_verified is True and result.restarted is True
            else:
                assert result is True
            await asyncio.sleep(0.15)
            assert len(read()) == (3 if queued_operation == 'reset' else 2)
            assert manager.is_running is (queued_operation != 'stop')
        finally:
            gate.unlink(missing_ok=True)
            for task in (reset, queued):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_reset_during_watchdog_backoff_has_only_one_replacement(manager_factory, monkeypatch):
    module, manager, _, read, _ = manager_factory
    monkeypatch.setattr(module, '_RESTART_RETRY_INITIAL', 0.3)

    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            first.kill()
            await first.wait()
            await asyncio.sleep(0.05)
            receipt = await manager.reset_tunnel()
            assert receipt.cutoff_verified is True and receipt.restarted is True
            await asyncio.sleep(0.4)
            assert len(read()) == 2 and manager.is_running
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_reset_cancelled_during_stop_holds_lock_through_cleanup(manager_factory):
    _, manager, bus, read, _ = manager_factory
    gate = manager._install_dir / 'term_gate'
    gate.touch()

    async def scenario():
        reset = None
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            reset = asyncio.create_task(manager.reset_tunnel())
            await wait_until(lambda: (manager._install_dir / 'term_received').exists())
            reset.cancel()
            asyncio.get_running_loop().call_later(0.03, reset.cancel)
            asyncio.get_running_loop().call_later(0.1, gate.unlink)
            with pytest.raises(asyncio.CancelledError):
                await reset
            assert first.returncode is not None and not manager.is_running
            assert manager._desired_token is None and not bus.listeners
            await asyncio.sleep(0.15)
            assert len(read()) == 1
        finally:
            gate.unlink(missing_ok=True)
            if reset is not None and not reset.done():
                reset.cancel()
                await asyncio.gather(reset, return_exceptions=True)
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_reset_cancelled_while_queued_does_not_change_active_reset(manager_factory):
    _, manager, bus, read, _ = manager_factory
    gate = manager._install_dir / 'term_gate'
    gate.touch()

    async def scenario():
        reset = queued = None
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            reset = asyncio.create_task(manager.reset_tunnel())
            await wait_until(lambda: (manager._install_dir / 'term_received').exists())
            queued = asyncio.create_task(manager.reset_tunnel())
            await asyncio.sleep(0.05)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            gate.unlink()
            receipt = await reset
            assert receipt.cutoff_verified is True and receipt.restarted is True
            assert manager.is_running and len(read()) == 2 and len(bus.listeners) == 1
        finally:
            gate.unlink(missing_ok=True)
            for task in (reset, queued):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_reset_without_process_or_during_shutdown_never_starts(manager_factory):
    _, manager, bus, read, _ = manager_factory

    async def scenario():
        receipt = await manager.reset_tunnel()
        assert receipt.cutoff_verified is True and receipt.restarted is False
        assert not read() and not bus.listeners
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            manager.hass.is_stopping = True
            receipt = await manager.reset_tunnel()
            assert receipt.cutoff_verified is True and receipt.restarted is False
            assert first.returncode is not None and len(read()) == 1
            assert not manager.is_running and not bus.listeners
        finally:
            await manager.stop_tunnel()
    asyncio.run(scenario())


@pytest.mark.parametrize('lookup_stage', ['term', 'kill'])
def test_reset_requires_observed_exit_after_signal_lookup_error(manager_factory, monkeypatch, lookup_stage):
    _, manager, bus, read, _ = manager_factory
    if lookup_stage == 'kill':
        (manager._install_dir / 'ignore_term').touch()

    async def scenario():
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            first = manager._process
            real_signal = first.send_signal
            real_kill = first.kill

            def unavailable_signal(sig):
                if lookup_stage == 'term' and sig == signal.SIGTERM:
                    raise ProcessLookupError
                real_signal(sig)

            # The OS boundary reports no such process, but the native handle
            # has no exit status yet. Reset must conservatively retain it.
            monkeypatch.setattr(first, 'send_signal', unavailable_signal)
            if lookup_stage == 'kill':
                def unavailable_kill():
                    raise ProcessLookupError
                monkeypatch.setattr(first, 'kill', unavailable_kill)
            receipt = await manager.reset_tunnel()
            assert receipt.cutoff_verified is False and receipt.restarted is False
            assert manager._process is first and first.returncode is None
            assert len(read()) == 1 and not bus.listeners
            assert not await manager.stop_tunnel()
            assert manager._process is first and first.returncode is None
            assert not await manager.start_tunnel('synthetic-watchdog-token')
            assert len(read()) == 1
            monkeypatch.setattr(first, 'send_signal', real_signal)
            monkeypatch.setattr(first, 'kill', real_kill)
            first.kill()
            await first.wait()
            assert await manager.stop_tunnel()
            (manager._install_dir / 'ignore_term').unlink(missing_ok=True)
            assert await manager.start_tunnel('synthetic-watchdog-token')
            assert len(read()) == 2
        finally:
            if 'first' in locals():
                monkeypatch.setattr(first, 'send_signal', real_signal)
                monkeypatch.setattr(first, 'kill', real_kill)
                if first.returncode is None:
                    first.kill()
                await first.wait()
            await manager.stop_tunnel()
    asyncio.run(scenario())


def test_reset_cancelled_failed_spawn_returns_no_receipt(manager_factory, monkeypatch):
    module, manager, bus, _, _ = manager_factory
    real_spawn = asyncio.create_subprocess_exec

    async def scenario():
        reset = None
        entered = asyncio.Event()
        release = asyncio.Event()
        try:
            assert await manager.start_tunnel('synthetic-watchdog-token')
            manager._binary_path.chmod(0o600)

            async def delayed_exec_error(*args, **kwargs):
                entered.set()
                await release.wait()
                return await real_spawn(*args, **kwargs)

            monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', delayed_exec_error)
            reset = asyncio.create_task(manager.reset_tunnel())
            await entered.wait()
            reset.cancel()
            asyncio.get_running_loop().call_later(0.05, release.set)
            with pytest.raises(asyncio.CancelledError):
                await reset
            assert not manager.is_running and not bus.listeners
            assert manager._desired_token is None
        finally:
            release.set()
            if reset is not None and not reset.done():
                reset.cancel()
                await asyncio.gather(reset, return_exceptions=True)
            await manager.stop_tunnel()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['timeout', 'lookup'])
def test_transport_probe_cleanup_failure_is_bounded_and_reports_all_handles(tmp_path, fault):
    """Only the failure cleanup seam is simulated; the 21-check OS proof stays real."""
    import ast
    import hashlib
    import tempfile

    probe = SOURCE.parent / 'scripts' / 'probe_connector_transport.py'
    tree = ast.parse(probe.read_text())
    main = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'main')

    class Handle:
        returncode = None

        def __init__(self, failure=None):
            self.failure = failure
            self.kill_attempted = False
            self.wait_attempted = False

        def kill(self):
            self.kill_attempted = True
            if self.failure == 'lookup':
                raise ProcessLookupError

        async def wait(self):
            self.wait_attempted = True
            if self.failure == 'timeout':
                await asyncio.sleep(60)
            self.returncode = -signal.SIGKILL
            return self.returncode

    handles = [Handle(fault), Handle('lookup'), Handle()]

    def fail_setup(*_):
        raise RuntimeError('synthetic setup failure to enter owned-handle fallback')

    namespace = {
        'asyncio': asyncio, 'hashlib': hashlib, 'json': json, 'Path': Path,
        'tempfile': tempfile, 'SOURCE': SOURCE, 'OUT': tmp_path,
        'load_manager': lambda: None, 'make_manager': fail_setup,
        'processes': handles, 'checks': [], '_CLEANUP_WAIT_TIMEOUT': 0.02,
    }
    # Execute the exact packaged main body, without importing WS/HA libraries
    # unrelated to this failure seam. Full packaged transport runs separately.
    exec(compile(ast.Module(body=[main], type_ignores=[]), str(probe), 'exec'), namespace)

    async def scenario():
        assert await asyncio.wait_for(namespace['main'](), timeout=0.3) == 1
        result = json.loads((tmp_path / 'result.json').read_text())
        assert result['passed'] is False and result['cleanup'] is False
        assert result['failure_type'] == 'RuntimeError' and result['runtime_removed'] is True
        assert all(handle.kill_attempted for handle in handles)
        assert handles[-1].wait_attempted and handles[-1].returncode == -signal.SIGKILL
        expected = ['TimeoutError', 'ProcessLookupError'] if fault == 'timeout' else ['ProcessLookupError', 'ProcessLookupError']
        assert result['cleanup_failure_types'] == expected
    asyncio.run(scenario())
