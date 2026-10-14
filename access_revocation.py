"""Smart Home 自動撤權：平台 ``access_revision`` 改變 ⇒ 背景斷線重連（跨 repo 契約 v1 §5）.

平台在 Cloudflare Access 收緊某條 route 之後會把 home 的 ``access_revision`` 往前推。
Access 收緊只擋**新的**登入，已經建立的長連線（WebSocket 等）不會被切；所以 HA 看到
數值改變，就用 :meth:`CloudflaredManager.reset_tunnel` 把整條隧道斷掉重連，讓既有
連線全部重新過一次 Access。代價是同一個家庭所有遠端連線短暫中斷（使用者 2026-10-14
已接受）。

刻意不 import Home Assistant、也不用相對 import：所有對外的東西（reset、時鐘、建立
背景任務）都由呼叫端注入，可攜式測試才能直接以檔案載入、跑真的 asyncio。

數值本身沒有意義，只能比較「一不一樣」：變小代表平台 DB 還原，一樣當作改變。
基準只放記憶體——HA 重啟本來就會重建所有連線，不需要持久化。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
import logging
import time
from typing import Any, Protocol

_LOGGER = logging.getLogger(__name__)


class _ResetResult(Protocol):
    """``cloudflared_manager.TunnelResetResult`` 的形狀（不 import，避免拉進 HA）."""

    cutoff_verified: bool
    restarted: bool


def parse_access_revision(value: object) -> int | None:
    """只收非負 int；bool（int 的子類）、負數、字串、None 一律視為沒有這個欄位."""
    if type(value) is int and value >= 0:
        return value
    return None


class AccessRevisionTracker:
    """追蹤平台撤權版本，落後就（合併、延遲、退避地）呼叫一次 reset."""

    def __init__(
        self,
        *,
        reset: Callable[[], Awaitable[_ResetResult]],
        has_cutoff_target: Callable[[], bool],
        is_deleted: Callable[[], bool],
        create_task: Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]],
        on_change: Callable[[], None],
        delay: float,
        retry_initial: float,
        retry_max: float,
        label: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the tracker; every collaborator is injected."""
        self._reset = reset
        self._has_cutoff_target = has_cutoff_target
        self._is_deleted = is_deleted
        self._create_task = create_task
        self._on_change = on_change
        self._delay = delay
        self._retry_initial = retry_initial
        self._retry_max = retry_max
        self._label = label
        self._now = now
        self._clock = clock

        # 最近一次成功 poll 看到的值（目標）與最近一次確認斷線時的目標值。
        self.revision: int | None = None
        self.applied: int | None = None
        # 最近一次真的呼叫 reset 的結果（時間＋兩個布林），從沒呼叫過為 None。
        self.last_reset: dict[str, Any] | None = None
        # 每看到一次「改變」就 +1。用序號而不是比數值判斷落後：A→B→A 這種
        # （DB 還原）數值回到已套用值，但中間 B 的變更可能已生效，仍要斷一次。
        self._seq = 0
        self._applied_seq = 0
        self._task: asyncio.Task[None] | None = None
        self._retry_delay = retry_initial
        self._retry_at = 0.0

    @property
    def pending(self) -> bool:
        """有看過、但還沒套用的改變."""
        return self._seq != self._applied_seq

    @property
    def attributes(self) -> dict[str, Any] | None:
        """給 tunnel status sensor 的 attributes；舊平台（從沒看過欄位）為 None."""
        if self.revision is None:
            return None
        return {
            "access_revision": self.revision,
            "access_revision_applied": self.applied,
            "last_access_reset": (
                dict(self.last_reset) if self.last_reset is not None else None
            ),
        }

    def async_poll(self, value: object = None) -> None:
        """每輪 Smart Home poll 呼叫一次（必須在 event loop）.

        成功的 poll 傳 ``access_revision`` 原值；失敗的 poll 傳 None——不帶新資訊，
        只讓先前失敗、退避到期的 reset 有機會重試。
        """
        revision = parse_access_revision(value)
        if revision is not None:
            if self.revision is None:
                # 第一次看到 ⇒ 基準，不斷線。
                self.revision = self.applied = revision
                return
            if revision != self.revision:
                self.revision = revision
                self._seq += 1
        self._maybe_start()

    def _maybe_start(self) -> None:
        if not self.pending:
            return
        if self._task is not None and not self._task.done():
            # 執行中：上面已經更新目標，跑完會自己再追一次（合併）。
            return
        if self._is_deleted() or self._clock() < self._retry_at:
            return
        self._task = self._create_task(
            self._async_run(), f"woow_access_revision_reset_{self._label}"
        )

    async def async_cancel(self) -> None:
        """取消執行中的背景 reset 並等它收尾（entry 卸載時呼叫）."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _async_run(self) -> None:
        while self.pending:
            # 等 Cloudflare 傳播。等待中又來新版本就再等一輪，讓這次 reset 涵蓋它；
            # poll 30 秒一次，所以最多多等一輪就會穩定。
            while True:
                seq = self._seq
                await asyncio.sleep(self._delay)
                if seq == self._seq:
                    break
            target = self.revision
            if self._is_deleted():
                # 平台上已刪除：隧道由 coordinator 停掉，這裡不再碰它。
                return
            if not self._has_cutoff_target():
                # 使用者手動停止（或還沒拉起來）⇒ 沒有連線可斷。
                _LOGGER.info(
                    "Access revision for %s changed to %s; no tunnel to cut, "
                    "marking it applied",
                    self._label,
                    target,
                )
                self._mark_applied(seq, target)
                continue

            _LOGGER.info(
                "Access revision for %s changed (%s -> %s); resetting the tunnel "
                "to drop existing remote connections",
                self._label,
                self.applied,
                target,
            )
            try:
                result = await self._reset()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                # 只記型別：例外訊息不保證不含路徑或參數。
                self._record(cutoff_verified=False, restarted=False)
                self._failed(type(err).__name__)
                return

            self._record(
                cutoff_verified=bool(result.cutoff_verified),
                restarted=bool(result.restarted),
            )
            if not result.cutoff_verified:
                self._failed("previous connector exit not verified")
                return
            self._mark_applied(seq, target)
            _LOGGER.info(
                "Tunnel reset for %s applied access revision %s (restarted=%s)",
                self._label,
                target,
                result.restarted,
            )
            if not result.restarted:
                # 斷線已確認，但新的 connector 沒起來；manager 不會自己重試，
                # 遠端連線要等使用者 start_tunnel 或重載整合才會恢復。
                _LOGGER.warning(
                    "Tunnel for %s was cut but not restarted; call the "
                    "start_tunnel service or reload the integration",
                    self._label,
                )

    def _record(self, *, cutoff_verified: bool, restarted: bool) -> None:
        self.last_reset = {
            "at": self._now().isoformat(),
            "cutoff_verified": cutoff_verified,
            "restarted": restarted,
        }
        self._on_change()

    def _mark_applied(self, seq: int, target: int | None) -> None:
        self._applied_seq = seq
        self.applied = target
        self._retry_delay = self._retry_initial
        self._retry_at = 0.0
        self._on_change()

    def _failed(self, reason: str) -> None:
        delay = self._retry_delay
        self._retry_at = self._clock() + delay
        self._retry_delay = min(delay * 2, self._retry_max)
        _LOGGER.warning(
            "Tunnel reset for %s access revision %s failed (%s); retrying on a "
            "later poll in at least %s seconds",
            self._label,
            self.revision,
            reason,
            delay,
        )
