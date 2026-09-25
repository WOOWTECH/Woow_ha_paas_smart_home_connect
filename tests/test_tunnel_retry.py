"""Tests for _async_start_tunnel_with_retry.

回歸守門：舊版在 bring-up 失敗時記一行 error 就放棄，造成整合永久半殘
（sensor 照跑、隧道永遠不起來、重開 HA 也不會自癒）。實機上是住家線路只有
4 KB/s、cloudflared 下載中斷觸發的。這裡釘住「會重試」與「退避會成長且有上限」。

``asyncio.sleep`` 全程被替換掉，所以測試不會真的等待；被要求的秒數記在
``slept`` 裡供斷言。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.woow_paas_smart_home import (
    _TUNNEL_RETRY_INITIAL,
    _TUNNEL_RETRY_MAX,
    _async_start_tunnel_with_retry,
)
import pytest


def _entry(token: str | None = "tok") -> MagicMock:
    entry = MagicMock()
    entry.data = {"tunnel_token": token} if token is not None else {}
    return entry


def _manager(binary: list[bool], start: list[bool]) -> MagicMock:
    mgr = MagicMock()
    mgr.ensure_binary = AsyncMock(side_effect=binary)
    mgr.start_tunnel = AsyncMock(side_effect=start)
    return mgr


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    """攔下所有 asyncio.sleep，記錄秒數而不真的等。"""
    recorded: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return recorded


async def test_succeeds_first_attempt(slept: list[float]) -> None:
    """一次就成功：各呼叫一次，除了啟動前那 1 秒外不再等待。"""
    mgr = _manager([True], [True])

    await _async_start_tunnel_with_retry(_entry(), mgr, "smart_home", 1)

    assert mgr.ensure_binary.await_count == 1
    assert mgr.start_tunnel.await_count == 1
    assert slept == [1]  # 只有開頭讓 HA 啟動完成的那一次


async def test_retries_until_binary_is_available(slept: list[float]) -> None:
    """下載連續失敗兩次後成功——舊版會在第一次就放棄。"""
    mgr = _manager([False, False, True], [True])

    await _async_start_tunnel_with_retry(_entry(), mgr, "smart_home", 1)

    assert mgr.ensure_binary.await_count == 3
    assert mgr.start_tunnel.await_count == 1
    # 1 秒啟動延遲 + 兩次退避
    assert slept == [1, _TUNNEL_RETRY_INITIAL, _TUNNEL_RETRY_INITIAL * 2]


async def test_retries_when_start_tunnel_fails(slept: list[float]) -> None:
    """二進位有了但 start_tunnel 失敗，同樣要重試。"""
    mgr = _manager([True, True], [False, True])

    await _async_start_tunnel_with_retry(_entry(), mgr, "smart_home", 1)

    assert mgr.start_tunnel.await_count == 2


async def test_retries_on_unexpected_exception(slept: list[float]) -> None:
    """非預期例外不可以終結重試（它同樣可能是暫時的網路問題）。"""
    mgr = MagicMock()
    mgr.ensure_binary = AsyncMock(side_effect=[OSError("boom"), True])
    mgr.start_tunnel = AsyncMock(return_value=True)

    await _async_start_tunnel_with_retry(_entry(), mgr, "smart_home", 1)

    assert mgr.ensure_binary.await_count == 2
    assert mgr.start_tunnel.await_count == 1


async def test_backoff_doubles_and_is_capped(slept: list[float]) -> None:
    """退避倍增但不超過上限——長時間斷線時不會把間隔拉到無限大。"""
    attempts = 12
    mgr = _manager([False] * (attempts - 1) + [True], [True])

    await _async_start_tunnel_with_retry(_entry(), mgr, "smart_home", 1)

    backoffs = slept[1:]  # 去掉開頭的 1 秒
    assert backoffs[0] == _TUNNEL_RETRY_INITIAL
    assert backoffs[1] == _TUNNEL_RETRY_INITIAL * 2
    assert max(backoffs) == _TUNNEL_RETRY_MAX
    assert backoffs == sorted(backoffs)  # 單調不遞減
    assert backoffs[-1] == _TUNNEL_RETRY_MAX


async def test_missing_token_gives_up_without_retrying(slept: list[float]) -> None:
    """缺 token 是設定問題不是網路問題，重試永遠不會好 → 立刻放棄。"""
    mgr = _manager([True], [True])

    await _async_start_tunnel_with_retry(_entry(token=None), mgr, "smart_home", 1)

    mgr.ensure_binary.assert_not_awaited()
    assert slept == [1]  # 沒有任何退避


async def test_cancellation_propagates(monkeypatch) -> None:
    """HA 關機／卸載會取消這個工作；不可以吞掉後繼續重試，否則會攔住關機。"""
    async def _sleep_then_cancel(seconds: float) -> None:
        if seconds != 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _sleep_then_cancel)
    mgr = _manager([False, True], [True])

    with pytest.raises(asyncio.CancelledError):
        await _async_start_tunnel_with_retry(_entry(), mgr, "smart_home", 1)


async def test_cancellation_during_download_is_not_swallowed(
    slept: list[float],
) -> None:
    """取消發生在 ensure_binary 裡時，也要往外傳而不是被當成一次失敗。"""
    mgr = MagicMock()
    mgr.ensure_binary = AsyncMock(side_effect=asyncio.CancelledError)
    mgr.start_tunnel = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await _async_start_tunnel_with_retry(_entry(), mgr, "smart_home", 1)
