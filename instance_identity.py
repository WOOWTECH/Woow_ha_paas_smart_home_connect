"""這台 Home Assistant 的身分，用來回答「這個家是誰綁的」（#31）。

一個家（或門禁）只能有一台 HA 接它的 Cloudflare tunnel：兩台同時接上同一條
tunnel 時，Cloudflare 會把進來的請求輪流送到其中一台，使用者會隨機登入到錯的
那台機器，而兩邊看起來都「正常連線」。平台端靠這裡產生的 uid 認人並拒發第二份
權杖；本機端靠它認出「設定被搬到另一台機器」。

uid 用 HA 自己的 instance id（``helpers.instance_id``，存在 ``.storage`` 裡、
重開機與升級都不變）。**備份還原到另一台機器會把它一起帶過去**，所以 uid 相同
不足以證明是同一台實體機器；真正要擋的那一類（同一個映像複製出去的展示機）
由平台端的「同一條 tunnel 出現兩個 cloudflared」偵測負責。兩層都需要。
"""

from __future__ import annotations

import socket

from homeassistant.core import HomeAssistant
from homeassistant.helpers import instance_id


async def async_instance_identity(hass: HomeAssistant) -> tuple[str, str]:
    """Return ``(uid, label)`` for this Home Assistant installation.

    label 只是給人看的（平台會把它顯示在「已綁定給誰」的訊息裡），所以取得失敗
    時退回空字串就好，不要讓它擋住設定流程。
    """
    uid = await instance_id.async_get(hass)
    return uid, _label(hass)


def _label(hass: HomeAssistant) -> str:
    """``位置名稱 (主機名)``；兩者都拿不到時回空字串。"""
    name = (hass.config.location_name or "").strip()
    try:
        host = socket.gethostname().strip()
    except OSError:  # pragma: no cover - 容器內極少發生
        host = ""
    if name and host and host.lower() not in name.lower():
        return f"{name} ({host})"
    return name or host
