"""Install the reviewed woowtech MCP component and create its native server entry.

Only an active Smart Home MCP subscription calls this helper. Existing component
files, disabled entries and authentication choices belong to the administrator.
New installs use the same component-cache refresh as HACS first installation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import hashlib
import importlib
import io
import json
import logging
from pathlib import Path, PurePosixPath
import stat
import tempfile
from typing import TYPE_CHECKING
import zipfile

import aiohttp
from awesomeversion import AwesomeVersion

from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import DATA_CUSTOM_COMPONENTS, async_get_custom_components

from .const import DOMAIN, HA_MCP_DOMAIN, MCP_SUBSCRIPTION_VALUE

if TYPE_CHECKING:
    from .coordinator import TunnelCoordinator

_LOGGER = logging.getLogger(__name__)
MIN_HA_VERSION = "2026.6.0"
MCP_COMMIT = "c7b8d79fea7228007d7af9291f3a03afc4d3e071"
MCP_ARCHIVE_URL = (
    "https://codeload.github.com/WOOWTECH/Woow_ha_ai_mcp/zip/" + MCP_COMMIT
)
MCP_ARCHIVE_SHA256 = "c162461ae8cad95a696212328a340838533bd8b59fa7326948f810cdd00bd4de"
_ARCHIVE_PREFIX = f"Woow_ha_ai_mcp-{MCP_COMMIT}/custom_components/{HA_MCP_DOMAIN}/"
_MAX_DOWNLOAD = 32 * 1024 * 1024
_MAX_EXTRACTED = 8 * 1024 * 1024
_DATA_LOCK = f"{DOMAIN}_mcp_install_lock"
DATA_INSTALLING = f"{DOMAIN}_mcp_installing"


class McpSetupError(Exception):
    """A known setup problem whose code has a translated repair message."""


def mcp_supported() -> bool:
    """The embedded server requires HA 2026.6 or newer."""
    return AwesomeVersion(HA_VERSION) >= AwesomeVersion(MIN_HA_VERSION)


def _install_archive(payload: bytes, component_dir: Path) -> None:
    """Validate before installing; never replace an existing component."""
    if hashlib.sha256(payload).hexdigest() != MCP_ARCHIVE_SHA256:
        raise McpSetupError("mcp_install_failed")
    component_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".woow-mcp-", dir=component_dir.parent
    ) as temporary:
        staged = Path(temporary) / HA_MCP_DOMAIN
        staged.mkdir()
        total = 0
        names: set[str] = set()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.infolist():
                if not member.filename.startswith(_ARCHIVE_PREFIX):
                    continue
                relative = member.filename[len(_ARCHIVE_PREFIX):]
                if not relative or member.is_dir():
                    continue
                path = PurePosixPath(relative)
                total += member.file_size
                if (
                    path.is_absolute() or ".." in path.parts or "\\" in relative
                    or stat.S_ISLNK(member.external_attr >> 16)
                    or relative in names or total > _MAX_EXTRACTED
                ):
                    raise McpSetupError("mcp_install_failed")
                names.add(relative)
                destination = staged.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(member))
        manifest = json.loads((staged / "manifest.json").read_text())
        if (
            manifest.get("domain") != HA_MCP_DOMAIN
            or manifest.get("version") != "1.2.2"
            or not (staged / "config_flow.py").is_file()
            or not (staged / "__init__.py").is_file()
        ):
            raise McpSetupError("mcp_install_failed")
        # A concurrent HACS installation wins; its files must be preserved.
        if component_dir.exists():
            return
        staged.rename(component_dir)


async def _async_download(hass: HomeAssistant) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async with async_get_clientsession(hass).get(
        MCP_ARCHIVE_URL, timeout=aiohttp.ClientTimeout(total=180)
    ) as response:
        response.raise_for_status()
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > _MAX_DOWNLOAD:
                raise McpSetupError("mcp_install_failed")
            chunks.append(chunk)
    return b"".join(chunks)


async def async_ensure_mcp_server(
    hass: HomeAssistant, eligible: Callable[[], bool] = lambda: True
) -> None:
    """Install once, then drive MCP's native user → server flow idempotently."""
    lock = hass.data.setdefault(_DATA_LOCK, asyncio.Lock())
    async with lock:
        if not eligible():
            return
        if not mcp_supported():
            raise McpSetupError("mcp_ha_unsupported")
        component_dir = Path(hass.config.path("custom_components", HA_MCP_DOMAIN))
        if not await hass.async_add_executor_job(component_dir.exists):
            hass.data[DATA_INSTALLING] = True
            try:
                payload = await _async_download(hass)
                if not eligible():
                    return
                await hass.async_add_executor_job(_install_archive, payload, component_dir)
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError, zipfile.BadZipFile) as err:
                raise McpSetupError("mcp_install_failed") from err
            finally:
                hass.data.pop(DATA_INSTALLING, None)

        if not eligible():
            return
        # Wait for any in-flight scan before invalidating its completed cache.
        components = await async_get_custom_components(hass)
        if HA_MCP_DOMAIN not in components:
            await hass.async_add_executor_job(importlib.invalidate_caches)
            hass.data.pop(DATA_CUSTOM_COMPONENTS, None)
            components = await async_get_custom_components(hass)
        if HA_MCP_DOMAIN not in components:
            raise McpSetupError("mcp_install_failed")

        servers = [
            item for item in hass.config_entries.async_entries(HA_MCP_DOMAIN)
            if item.data.get("entry_type") == "server"
        ]
        if servers:
            server = servers[0]
            if server.disabled_by is not None:
                raise McpSetupError("mcp_disabled")
            if server.options.get("enable_webhook", True) is False:
                raise McpSetupError("mcp_webhook_disabled")
            # Native bring-up runs in the background and may fail while its
            # entry remains LOADED. Retry that failure on the next managed pass,
            # but never reload during installation or while a server is healthy.
            runtime = hass.data.get(HA_MCP_DOMAIN, {})
            task = runtime.get("bringup_task")
            manager = runtime.get("manager")
            running = manager is not None and manager.is_running is True and bool(runtime.get("webhook"))
            state = getattr(server, "state", None)
            if state == ConfigEntryState.SETUP_ERROR or (
                state == ConfigEntryState.LOADED and not running
                and (task is None or task.done())
            ):
                await hass.config_entries.async_reload(server.entry_id)
            # Never replace its authentication options or rotate existing secrets.
            return
        if not eligible():
            return
        result = await hass.config_entries.flow.async_init(
            HA_MCP_DOMAIN, context={"source": SOURCE_USER}
        )
        flow_id = result.get("flow_id")
        try:
            if result["type"] == "menu" and "server" in result.get("menu_options", []):
                result = await hass.config_entries.flow.async_configure(
                    result["flow_id"], {"next_step_id": "server"}
                )
            if result["type"] == "form" and result.get("step_id") == "server" and eligible():
                result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
            if result["type"] == "create_entry":
                return
            if result["type"] == "abort" and result.get("reason") == "already_configured":
                return
            raise McpSetupError("mcp_setup_failed")
        finally:
            if flow_id and any(
                flow["flow_id"] == flow_id
                for flow in hass.config_entries.flow.async_progress()
            ):
                hass.config_entries.flow.async_abort(flow_id)


async def async_manage_mcp(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: TunnelCoordinator
) -> None:
    """Resume after restart/network loss; do not block the remote access tunnel."""
    issue_id = f"mcp_setup_{entry.entry_id}"

    def eligible() -> bool:
        data = coordinator.data
        return bool(data and not data.instance_deleted and data.mcp == MCP_SUBSCRIPTION_VALUE)

    try:
        while True:
            if eligible():
                try:
                    await async_ensure_mcp_server(hass, eligible)
                except Exception as err:  # A failed optional MCP must not stop the tunnel.
                    reason = str(err) if isinstance(err, McpSetupError) else "mcp_setup_failed"
                    _LOGGER.warning("MCP setup pending (%s)", reason)
                    ir.async_create_issue(
                        hass, DOMAIN, issue_id, is_fixable=False,
                        severity=ir.IssueSeverity.WARNING, translation_key=reason,
                    )
                else:
                    ir.async_delete_issue(hass, DOMAIN, issue_id)
            else:
                ir.async_delete_issue(hass, DOMAIN, issue_id)
            await asyncio.sleep(300)
    finally:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
