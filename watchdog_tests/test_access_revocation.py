"""撤權版本追蹤（AccessRevisionTracker）的可攜式測試：真 asyncio、無 HA、無真 token.

前半用假 manager 驗合約語意（基準、延遲、合併、退避、停止、刪除、取消）；最後一支
接上真的 CloudflaredManager 與假 cloudflared 子行程，驗證 reset 真的換掉了子行程。
延遲縮短為幾十毫秒；退避改用注入的時鐘驗 30 → 300 秒的實際數列，不靠真的等待。
"""
import asyncio
from datetime import datetime
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import types
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get(
    'SHC_ACCESS_REVOCATION_SOURCE', str(ROOT / 'access_revocation.py')))
MANAGER_SOURCE = Path(os.environ.get(
    'SHC_MANAGER_SOURCE', str(ROOT / 'cloudflared_manager.py')))

DELAY = 0.05
TOKEN = 'synthetic-access-revocation-token'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tracker_module = _load('access_revocation_under_test', SOURCE)


class Result(NamedTuple):
    cutoff_verified: bool
    restarted: bool


class FakeManager:
    """只記錄呼叫；結果依序取自 results（例外則 raise），可用 gate 卡住 reset."""

    def __init__(self):
        self.calls = 0
        self.results = []
        self.gate = None
        self.has_cutoff_target = True
        self.cancelled = 0

    async def reset_tunnel(self):
        self.calls += 1
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        result = self.results.pop(0) if self.results else Result(True, True)
        if isinstance(result, BaseException):
            raise result
        return result


class Harness:
    def __init__(self, manager=None, *, delay=DELAY, clock=None, retry=(30, 300)):
        self.manager = manager or FakeManager()
        self.deleted = False
        self.changes = 0
        self.tasks = []
        kwargs = {} if clock is None else {'clock': clock}
        self.tracker = tracker_module.AccessRevisionTracker(
            reset=self.manager.reset_tunnel,
            has_cutoff_target=lambda: self.manager.has_cutoff_target,
            is_deleted=lambda: self.deleted,
            create_task=self._create_task,
            on_change=self._changed,
            delay=delay,
            retry_initial=retry[0],
            retry_max=retry[1],
            label='smart_home 6',
            **kwargs,
        )

    def _create_task(self, coro, name):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task

    def _changed(self):
        self.changes += 1

    async def settle(self):
        """等所有背景 reset 結束（含合併後追加的那一輪）."""
        async with asyncio.timeout(5):
            while any(not task.done() for task in self.tasks):
                await asyncio.sleep(0.01)

    async def close(self):
        await self.tracker.async_cancel()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def wait_until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def run(scenario):
    async def wrapper():
        harness = Harness()
        try:
            await scenario(harness)
        finally:
            await harness.close()
    asyncio.run(wrapper())


def test_first_observation_is_baseline_without_reset():
    async def scenario(h):
        h.tracker.async_poll(7)
        h.tracker.async_poll(7)
        await asyncio.sleep(DELAY * 3)
        assert h.manager.calls == 0 and h.tasks == []
        assert h.tracker.attributes == {
            'access_revision': 7, 'access_revision_applied': 7,
            'last_access_reset': None,
        }
    run(scenario)


def test_change_resets_exactly_once_after_delay():
    async def scenario(h):
        h.tracker.async_poll(7)
        h.tracker.async_poll(8)
        # 還在等 Cloudflare 傳播，尚未呼叫。
        await asyncio.sleep(DELAY / 3)
        assert h.manager.calls == 0
        await h.settle()
        assert h.manager.calls == 1
        # 同值的後續 poll 不再觸發。
        h.tracker.async_poll(8)
        await asyncio.sleep(DELAY * 3)
        assert h.manager.calls == 1 and len(h.tasks) == 1
        attrs = h.tracker.attributes
        assert attrs['access_revision'] == attrs['access_revision_applied'] == 8
        last = attrs['last_access_reset']
        assert last['cutoff_verified'] is True and last['restarted'] is True
        assert datetime.fromisoformat(last['at']).tzinfo is not None
        assert h.changes >= 1
    run(scenario)


def test_bumps_during_inflight_reset_coalesce_into_one_more_run():
    async def scenario(h):
        h.manager.gate = asyncio.Event()
        h.tracker.async_poll(1)
        h.tracker.async_poll(2)
        await wait_until(lambda: h.manager.calls == 1)
        for value in (3, 4, 5):
            h.tracker.async_poll(value)
        await asyncio.sleep(DELAY * 3)
        assert h.manager.calls == 1 and len(h.tasks) == 1
        h.manager.gate.set()
        await h.settle()
        # 第一次只算 2；仍落後 ⇒ 同一個背景任務再追一次，直接涵蓋最新的 5。
        assert h.manager.calls == 2 and len(h.tasks) == 1
        assert h.tracker.applied == 5 and not h.tracker.pending
    run(scenario)


def test_bump_during_propagation_delay_is_covered_by_same_reset():
    async def scenario(h):
        h.tracker.async_poll(1)
        h.tracker.async_poll(2)
        await asyncio.sleep(DELAY / 2)
        h.tracker.async_poll(3)
        await h.settle()
        assert h.manager.calls == 1 and h.tracker.applied == 3
    run(scenario)


@pytest.mark.parametrize('sequence,applied', [((5, 3), 3), ((5, 0), 0)])
def test_decrease_is_treated_as_change(sequence, applied):
    async def scenario(h):
        for value in sequence:
            h.tracker.async_poll(value)
        await h.settle()
        assert h.manager.calls == 1 and h.tracker.applied == applied
    run(scenario)


def test_return_to_applied_value_mid_reset_still_resets_again():
    """A→B→A（DB 還原）：數值回到已套用值，但中間 B 可能已生效，仍要再斷一次."""
    async def scenario(h):
        h.manager.gate = asyncio.Event()
        h.tracker.async_poll(5)
        h.tracker.async_poll(6)
        await wait_until(lambda: h.manager.calls == 1)
        h.tracker.async_poll(5)
        h.manager.gate.set()
        await h.settle()
        assert h.manager.calls == 2 and h.tracker.applied == 5
        assert not h.tracker.pending
    run(scenario)


@pytest.mark.parametrize('value', [None, True, False, -1, '3', 3.0, [3], {'v': 3}])
def test_absent_or_invalid_revision_is_ignored(value):
    async def scenario(h):
        h.tracker.async_poll(value)
        assert h.tracker.attributes is None
        h.tracker.async_poll(4)
        h.tracker.async_poll(value)
        await asyncio.sleep(DELAY * 3)
        assert h.manager.calls == 0 and h.tasks == []
        assert h.tracker.revision == h.tracker.applied == 4
    run(scenario)


def test_transient_poll_failure_triggers_nothing():
    async def scenario(h):
        h.tracker.async_poll(9)
        for _ in range(3):
            h.tracker.async_poll()  # coordinator 在失敗 poll 傳 None
        await asyncio.sleep(DELAY * 3)
        assert h.manager.calls == 0 and h.tasks == []
        assert h.tracker.applied == 9
    run(scenario)


def test_stopped_tunnel_marks_applied_without_reset():
    async def scenario(h):
        h.manager.has_cutoff_target = False
        h.tracker.async_poll(1)
        h.tracker.async_poll(2)
        await h.settle()
        assert h.manager.calls == 0
        assert h.tracker.applied == 2 and h.tracker.last_reset is None
        assert h.changes >= 1  # sensor 立刻看到已套用
    run(scenario)


def test_deleted_instance_never_resets():
    async def scenario(h):
        h.deleted = True
        h.tracker.async_poll(1)
        h.tracker.async_poll(2)
        await asyncio.sleep(DELAY * 3)
        assert h.manager.calls == 0 and h.tasks == []
        # 等待傳播期間被判定刪除，同樣不碰隧道。
        h.deleted = False
        h.tracker.async_poll(3)
        await asyncio.sleep(DELAY / 3)
        h.deleted = True
        await h.settle()
        assert h.manager.calls == 0 and h.tracker.applied == 1
    run(scenario)


def test_cancel_during_delay_prevents_reset_and_allows_later_retry():
    async def scenario(h):
        h.tracker.async_poll(1)
        h.tracker.async_poll(2)
        assert len(h.tasks) == 1 and not h.tasks[0].done()
        await h.tracker.async_cancel()
        assert h.tasks[0].cancelled()
        await asyncio.sleep(DELAY * 3)
        assert h.manager.calls == 0 and h.tracker.applied == 1
        # 取消不算失敗：沒有退避，下一輪 poll 照常啟動。
        h.tracker.async_poll(2)
        await h.settle()
        assert h.manager.calls == 1 and h.tracker.applied == 2
    run(scenario)


def test_cancel_during_inflight_reset_propagates_and_does_not_advance():
    async def scenario(h):
        h.manager.gate = asyncio.Event()
        h.tracker.async_poll(1)
        h.tracker.async_poll(2)
        await wait_until(lambda: h.manager.calls == 1)
        await h.tracker.async_cancel()
        assert h.manager.cancelled == 1 and h.tasks[0].cancelled()
        assert h.tracker.applied == 1 and h.tracker.last_reset is None
        # 已取消的 tracker 不會自己再起任務。
        await asyncio.sleep(DELAY * 3)
        assert len(h.tasks) == 1
    run(scenario)


def test_failure_does_not_advance_and_retries_with_capped_backoff():
    """失敗（未驗證或例外）不前進；只在之後的 poll、退避到期才重試：30→60→…→300."""
    async def scenario():
        now = [1000.0]
        h = Harness(clock=lambda: now[0])
        h.manager.results = [
            Result(False, False), RuntimeError('synthetic'), Result(False, False),
            OSError('synthetic'), Result(False, False), Result(False, False),
            Result(True, False),
        ]
        try:
            h.tracker.async_poll(1)
            h.tracker.async_poll(2)
            await h.settle()
            assert h.manager.calls == 1 and h.tracker.applied == 1
            assert h.tracker.last_reset['cutoff_verified'] is False
            for expected_wait in (30, 60, 120, 240, 300, 300):
                calls = h.manager.calls
                now[0] += expected_wait - 1
                h.tracker.async_poll(2)
                await asyncio.sleep(DELAY * 2)
                assert h.manager.calls == calls, expected_wait  # 退避未到
                now[0] += 1
                h.tracker.async_poll(2)
                await h.settle()
                assert h.manager.calls == calls + 1, expected_wait
            # 第 7 次成功（斷線已確認、未重啟）才前進。
            assert h.tracker.applied == 2 and not h.tracker.pending
            assert h.tracker.last_reset['cutoff_verified'] is True
            assert h.tracker.last_reset['restarted'] is False
            # 成功後退避歸零：下一個版本立刻（延遲後）處理。
            h.tracker.async_poll(3)
            await h.settle()
            assert h.manager.calls == 8 and h.tracker.applied == 3
        finally:
            await h.close()
    asyncio.run(scenario())


def test_failed_poll_can_retry_a_pending_reset_once_backoff_expires():
    async def scenario():
        now = [0.0]
        h = Harness(clock=lambda: now[0])
        h.manager.results = [Result(False, False)]
        try:
            h.tracker.async_poll(1)
            h.tracker.async_poll(2)
            await h.settle()
            now[0] += 30
            h.tracker.async_poll()  # 平台暫時連不上，但已知目標仍落後
            await h.settle()
            assert h.manager.calls == 2 and h.tracker.applied == 2
        finally:
            await h.close()
    asyncio.run(scenario())


def test_unconfirmed_change_reverted_during_backoff_still_resets():
    """5→6 斷線失敗，退避中又變回 5：6 那次可能已在 Cloudflare 生效，仍欠一次斷線."""
    async def scenario():
        now = [0.0]
        h = Harness(clock=lambda: now[0])
        h.manager.results = [Result(False, False)]
        try:
            h.tracker.async_poll(5)
            h.tracker.async_poll(6)
            await h.settle()
            h.tracker.async_poll(5)
            await asyncio.sleep(DELAY * 2)
            assert h.manager.calls == 1 and h.tracker.pending
            now[0] += 30
            h.tracker.async_poll(5)
            await h.settle()
            assert h.manager.calls == 2 and h.tracker.applied == 5
            assert not h.tracker.pending
        finally:
            await h.close()
    asyncio.run(scenario())


def test_failure_logs_warning_without_exception_text(caplog):
    async def scenario(h):
        h.manager.results = [RuntimeError(f'leaky {TOKEN}')]
        h.tracker.async_poll(1)
        h.tracker.async_poll(2)
        await h.settle()
    with caplog.at_level(logging.INFO):
        run(scenario)
    assert TOKEN not in caplog.text
    levels = {(r.levelno, r.getMessage().split(' ')[0]) for r in caplog.records}
    assert (logging.INFO, 'Access') in levels
    assert any(r.levelno == logging.WARNING and 'RuntimeError' in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------------------
# 接上真的 CloudflaredManager：假 cloudflared 子行程，只有 HA wiring 是 stub。
# ---------------------------------------------------------------------------

@pytest.fixture
def real_manager(monkeypatch, tmp_path):
    for name in ['homeassistant', 'homeassistant.core', 'homeassistant.const',
                 'homeassistant.helpers', 'homeassistant.helpers.aiohttp_client']:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules['homeassistant.core'].HomeAssistant = object
    sys.modules['homeassistant.const'].EVENT_HOMEASSISTANT_STOP = 'homeassistant_stop'
    sys.modules['homeassistant.helpers.aiohttp_client'].async_get_clientsession = lambda _: None
    module = _load('manager_for_access_revocation', MANAGER_SOURCE)
    records = tmp_path / 'spawn.jsonl'
    executable = tmp_path / 'cloudflared'
    executable.write_text("#!/usr/bin/env python3\n" + f'''
import json,os,pathlib,time
with pathlib.Path({str(records)!r}).open('a') as stream:
    stream.write(json.dumps({{'pid':os.getpid()}})+'\\n')
while True: time.sleep(0.03)
''')
    executable.chmod(0o700)

    class Bus:
        def async_listen_once(self, event, callback):
            return lambda: None

    hass = types.SimpleNamespace(bus=Bus(), is_stopping=False)
    manager = module.CloudflaredManager(hass, install_dir=str(tmp_path))

    def spawned():
        if not records.exists():
            return []
        return [json.loads(line)['pid'] for line in records.read_text().splitlines()]
    return manager, spawned


def test_real_manager_reset_replaces_child_and_reports_receipt(real_manager, caplog):
    manager, spawned = real_manager

    async def scenario():
        h = Harness(manager)
        try:
            assert manager.has_cutoff_target is False  # 從未啟動
            assert await manager.start_tunnel(TOKEN)
            assert manager.has_cutoff_target is True
            first = manager._process
            h.tracker.async_poll(10)
            h.tracker.async_poll(11)
            await h.settle()
            assert first.returncode is not None  # 舊 connector 真的退出
            assert manager.is_running and manager._process.pid != first.pid
            assert spawned() == [first.pid, manager._process.pid]
            assert h.tracker.applied == 11
            last = h.tracker.attributes['last_access_reset']
            assert (last['cutoff_verified'], last['restarted']) == (True, True)
            assert TOKEN not in json.dumps(h.tracker.attributes)

            # 手動停止後再撤權：沒有連線可斷，不會偷偷把隧道拉回來。
            assert await manager.stop_tunnel()
            assert manager.has_cutoff_target is False
            h.tracker.async_poll(12)
            await h.settle()
            assert h.tracker.applied == 12 and not manager.is_running
            assert len(spawned()) == 2
        finally:
            await h.close()
            await manager.stop_tunnel()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())
    assert TOKEN not in caplog.text
