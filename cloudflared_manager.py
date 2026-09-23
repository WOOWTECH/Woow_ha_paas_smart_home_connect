"""Cloudflared binary download and tunnel process lifecycle manager."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import platform
import shutil
import signal
import tarfile
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Default install directory relative to HA config dir
_DEFAULT_INSTALL_DIR = ".woow_paas_smart_home"

# Binary filename after extraction / download
_BINARY_NAME = "cloudflared"

# 下載 cloudflared（linux-amd64 約 39MB）**必須自帶 timeout**。
# HA 的共用 client session 帶著它自己的預設 timeout，對這種大檔案不夠用：實機上
# 這裡曾經在 `resp.read()` 讀到一半被計時器掐掉（aiohttp 的 TimerContext 會把
# CancelledError 轉成 TimeoutError），結果是安裝目錄建好了、二進位卻沒落地，
# 隧道從此起不來。
#
# **刻意不設 total**：檔案大小固定但線路速度不固定。實機量到對 GitHub releases
# 只有約 46 KB/s（39MB 要走十幾分鐘），任何「合理」的 total 都會在慢線路上變成
# 新的假超時——而那正是本來要修的病。改由 sock_read 把關：只要 60 秒內有收到下一個
# 區塊就繼續，真的斷線才會失敗。這樣慢歸慢會完成，斷線也不會無限掛著。
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=None, sock_connect=30, sock_read=60
)

# 邊收邊寫的區塊大小。不用 `resp.read()` 一次讀進記憶體：39MB 對 Pi 這類機器是
# 不必要的壓力，而且整包讀完才寫檔會讓失敗集中在最後一刻。
_DOWNLOAD_CHUNK = 256 * 1024

# Platform -> download URL mapping
_DOWNLOAD_URL_MAP: dict[tuple[str, str], str] = {
    ("Linux", "x86_64"): (
        "https://github.com/cloudflare/cloudflared/releases/latest"
        "/download/cloudflared-linux-amd64"
    ),
    ("Linux", "aarch64"): (
        "https://github.com/cloudflare/cloudflared/releases/latest"
        "/download/cloudflared-linux-arm64"
    ),
    ("Darwin", "x86_64"): (
        "https://github.com/cloudflare/cloudflared/releases/latest"
        "/download/cloudflared-darwin-amd64.tgz"
    ),
    ("Darwin", "arm64"): (
        "https://github.com/cloudflare/cloudflared/releases/latest"
        "/download/cloudflared-darwin-arm64.tgz"
    ),
}


class CloudflaredManager:
    """Manage cloudflared binary download and tunnel process lifecycle."""

    def __init__(
        self, hass: HomeAssistant, install_dir: str | None = None
    ) -> None:
        """Initialize manager.

        Args:
            hass: Home Assistant instance.
            install_dir: Directory to store cloudflared binary.
                         Defaults to ``{config_dir}/.woow_paas_smart_home/``.

        """
        self.hass = hass

        if install_dir is not None:
            self._install_dir = Path(install_dir)
        else:
            self._install_dir = Path(
                hass.config.path(_DEFAULT_INSTALL_DIR)
            )

        self._binary_path: Path = self._install_dir / _BINARY_NAME
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Binary management
    # ------------------------------------------------------------------

    async def ensure_binary(self) -> bool:
        """Ensure cloudflared binary exists. Download if not present.

        Returns ``True`` when a usable binary is available.
        """
        if self._binary_path.is_file():
            _LOGGER.debug(
                "cloudflared binary already exists at %s", self._binary_path
            )
            return True

        system = platform.system()
        machine = platform.machine()
        key = (system, machine)

        url = _DOWNLOAD_URL_MAP.get(key)
        if url is None:
            _LOGGER.error(
                "Unsupported platform: system=%s, machine=%s", system, machine
            )
            return False

        try:
            # Ensure install directory exists
            await self.hass.async_add_executor_job(
                self._install_dir.mkdir, 0o755, True, True  # mode, parents, exist_ok
            )

            _LOGGER.info(
                "Downloading cloudflared for %s/%s from GitHub releases",
                system,
                machine,
            )

            is_tgz = url.endswith(".tgz")

            if is_tgz:
                await self._download_and_extract_tgz(url)
            else:
                await self._download_binary(url)

            # Set executable permission
            await self.hass.async_add_executor_job(
                os.chmod, str(self._binary_path), 0o755
            )

            _LOGGER.info(
                "cloudflared binary ready at %s", self._binary_path
            )
        except (aiohttp.ClientError, tarfile.TarError, PermissionError, OSError):
            _LOGGER.exception("Failed to download cloudflared binary")
            return False
        else:
            return True

    async def _download_binary(self, url: str) -> None:
        """Download a raw binary file (Linux)."""
        session = async_get_clientsession(self.hass)
        async with session.get(
            url, allow_redirects=True, timeout=_DOWNLOAD_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            await self._stream_to_file(resp, self._binary_path)

    async def _stream_to_file(
        self, resp: aiohttp.ClientResponse, dest: Path
    ) -> None:
        """把回應內容邊收邊寫到 ``dest``，全部寫完才就位。

        先寫 ``dest.part`` 再 rename 到 ``dest``——rename 在同一個檔案系統上是原子的。
        這一步是必要的，不是潔癖：``ensure_binary()`` 只用 ``is_file()`` 判斷二進位
        在不在，所以一個下載到一半就中斷的檔案會被當成「已安裝」，之後每次啟動都拿
        那個壞檔去執行、而且永遠不會重新下載。寫暫存檔則是要嘛沒有、要嘛完整。
        """
        part = dest.with_name(dest.name + ".part")

        def _write(handle: Any, chunk: bytes) -> None:
            handle.write(chunk)

        handle = await self.hass.async_add_executor_job(part.open, "wb")
        try:
            async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK):
                await self.hass.async_add_executor_job(_write, handle, chunk)
        except BaseException:
            # 包含 CancelledError：HA 關機或 config entry 卸載會取消這個背景工作，
            # 不清掉的話下次啟動會看到一個半截的 .part 檔留在那裡。
            await self.hass.async_add_executor_job(handle.close)
            await self.hass.async_add_executor_job(part.unlink, True)
            raise
        await self.hass.async_add_executor_job(handle.close)
        await self.hass.async_add_executor_job(part.replace, dest)

    async def _download_and_extract_tgz(self, url: str) -> None:
        """Download a .tgz archive and extract the binary (macOS)."""
        tgz_path = self._install_dir / "cloudflared.tgz"

        # Download archive（同 _download_binary：自帶 timeout + 串流 + 原子搬移）
        session = async_get_clientsession(self.hass)
        async with session.get(
            url, allow_redirects=True, timeout=_DOWNLOAD_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            await self._stream_to_file(resp, tgz_path)

        # Extract and clean up in executor
        def _extract_and_cleanup() -> None:
            with tarfile.open(str(tgz_path), "r:gz") as tar:
                tar.extractall(
                    path=str(self._install_dir), filter="data"
                )
            tgz_path.unlink()

        await self.hass.async_add_executor_job(_extract_and_cleanup)

    async def cleanup_binary(self) -> None:
        """Remove the install directory and its contents."""
        if self._install_dir.is_dir():
            try:
                await self.hass.async_add_executor_job(
                    shutil.rmtree, str(self._install_dir)
                )
                _LOGGER.info(
                    "Removed cloudflared install directory %s",
                    self._install_dir,
                )
            except OSError:
                _LOGGER.warning(
                    "Failed to remove cloudflared install directory %s",
                    self._install_dir,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Tunnel lifecycle
    # ------------------------------------------------------------------

    async def start_tunnel(self, tunnel_token: str) -> bool:
        """Start cloudflared tunnel with the given token.

        Args:
            tunnel_token: Cloudflare tunnel token.

        Returns ``True`` when the process was started successfully.

        """
        async with self._lock:
            return await self._start_tunnel_locked(tunnel_token)

    async def _start_tunnel_locked(self, tunnel_token: str) -> bool:
        """Start tunnel (must be called while holding self._lock)."""
        if self.is_running:
            _LOGGER.warning("cloudflared tunnel is already running")
            return True

        if not self._binary_path.is_file():
            _LOGGER.error(
                "cloudflared binary not found at %s; call ensure_binary() first",
                self._binary_path,
            )
            return False

        cmd = [
            str(self._binary_path),
            "tunnel",
            "run",
            "--token",
            tunnel_token,
        ]

        # Log with masked token
        _LOGGER.info(
            "Starting cloudflared tunnel: %s tunnel run --token ****",
            self._binary_path,
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Start background tasks to stream stdout/stderr to logger
            self._stdout_task = asyncio.create_task(
                self._stream_output(self._process.stdout, logging.DEBUG)
            )
            self._stderr_task = asyncio.create_task(
                self._stream_output(self._process.stderr, logging.WARNING)
            )

            # Give the process a moment to start
            await asyncio.sleep(1)

            if self._process.returncode is not None:
                _LOGGER.error(
                    "cloudflared process exited immediately with code %s",
                    self._process.returncode,
                )
                self._process = None
                return False

            _LOGGER.info(
                "cloudflared tunnel started with PID %s", self._process.pid
            )
        except OSError:
            _LOGGER.exception("Failed to start cloudflared tunnel")
            self._process = None
            return False
        else:
            return True

    async def stop_tunnel(self) -> bool:
        """Stop the running cloudflared process gracefully.

        Sends SIGTERM first, waits up to 5 seconds, then SIGKILL.

        Returns ``True`` when the process has been stopped (or was not running).
        """
        async with self._lock:
            return await self._stop_tunnel_locked()

    async def _stop_tunnel_locked(self) -> bool:
        """Stop tunnel (must be called while holding self._lock)."""
        if self._process is None:
            _LOGGER.debug("No cloudflared process to stop")
            return True

        if self._process.returncode is not None:
            _LOGGER.debug(
                "cloudflared process already exited with code %s",
                self._process.returncode,
            )
            self._process = None
            self._cancel_stream_tasks()
            return True

        pid = self._process.pid
        _LOGGER.info("Stopping cloudflared tunnel (PID %s)", pid)

        try:
            self._process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            _LOGGER.debug("cloudflared process already gone")
            self._process = None
            self._cancel_stream_tasks()
            return True

        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
            _LOGGER.info("cloudflared tunnel stopped gracefully")
        except TimeoutError:
            _LOGGER.warning(
                "cloudflared did not stop within 5s, sending SIGKILL"
            )
            try:
                self._process.kill()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
                _LOGGER.info("cloudflared tunnel killed")
            except ProcessLookupError:
                _LOGGER.debug("cloudflared process already gone after SIGKILL")
            except TimeoutError:
                _LOGGER.error(
                    "cloudflared process (PID %s) did not terminate after "
                    "SIGKILL; the process may still be running",
                    pid,
                )

        self._process = None
        self._cancel_stream_tasks()
        return True

    def _cancel_stream_tasks(self) -> None:
        """Cancel stdout/stderr streaming tasks."""
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._stdout_task = None
        self._stderr_task = None

    @property
    def is_running(self) -> bool:
        """Check if cloudflared process is alive (non-blocking)."""
        return self._process is not None and self._process.returncode is None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _stream_output(
        stream: asyncio.StreamReader | None, level: int
    ) -> None:
        """Read lines from an asyncio stream and send them to the logger."""
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            _LOGGER.log(level, "[cloudflared] %s", line.decode().rstrip())
