"""Cloudflared binary download and tunnel process lifecycle manager."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import platform
import signal
import tarfile

import aiohttp

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Default install directory relative to HA config dir
_DEFAULT_INSTALL_DIR = ".woow_paas_smart_home"

# Binary filename after extraction / download
_BINARY_NAME = "cloudflared"

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
                self._install_dir.mkdir, True, True  # parents, exist_ok
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
            return True

        except Exception:
            _LOGGER.exception("Failed to download cloudflared binary")
            return False

    async def _download_binary(self, url: str) -> None:
        """Download a raw binary file (Linux)."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as resp:
                resp.raise_for_status()
                data = await resp.read()

        await self.hass.async_add_executor_job(
            self._binary_path.write_bytes, data
        )

    async def _download_and_extract_tgz(self, url: str) -> None:
        """Download a .tgz archive and extract the binary (macOS)."""
        tgz_path = self._install_dir / "cloudflared.tgz"

        # Download archive
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as resp:
                resp.raise_for_status()
                data = await resp.read()

        await self.hass.async_add_executor_job(tgz_path.write_bytes, data)

        # Extract and clean up in executor
        def _extract_and_cleanup() -> None:
            with tarfile.open(str(tgz_path), "r:gz") as tar:
                tar.extractall(path=str(self._install_dir))  # noqa: S202
            tgz_path.unlink()

        await self.hass.async_add_executor_job(_extract_and_cleanup)

    # ------------------------------------------------------------------
    # Tunnel lifecycle
    # ------------------------------------------------------------------

    async def start_tunnel(self, tunnel_token: str) -> bool:
        """Start cloudflared tunnel with the given token.

        Args:
            tunnel_token: Cloudflare tunnel token.

        Returns ``True`` when the process was started successfully.
        """
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
            asyncio.ensure_future(
                self._stream_output(self._process.stdout, logging.DEBUG)
            )
            asyncio.ensure_future(
                self._stream_output(self._process.stderr, logging.DEBUG)
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
            return True

        except Exception:
            _LOGGER.exception("Failed to start cloudflared tunnel")
            self._process = None
            return False

    async def stop_tunnel(self) -> bool:
        """Stop the running cloudflared process gracefully.

        Sends SIGTERM first, waits up to 5 seconds, then SIGKILL.

        Returns ``True`` when the process has been stopped (or was not running).
        """
        if self._process is None:
            _LOGGER.debug("No cloudflared process to stop")
            return True

        if self._process.returncode is not None:
            _LOGGER.debug(
                "cloudflared process already exited with code %s",
                self._process.returncode,
            )
            self._process = None
            return True

        pid = self._process.pid
        _LOGGER.info("Stopping cloudflared tunnel (PID %s)", pid)

        try:
            self._process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            _LOGGER.debug("cloudflared process already gone")
            self._process = None
            return True

        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
            _LOGGER.info("cloudflared tunnel stopped gracefully")
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "cloudflared did not stop within 5s, sending SIGKILL"
            )
            try:
                self._process.kill()
                await self._process.wait()
            except ProcessLookupError:
                pass
            _LOGGER.info("cloudflared tunnel killed")

        self._process = None
        return True

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
