"""Notifications (SPEC §10): Windows toast, phone push, webhook."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from typing import Any, Dict, List, Optional

log = logging.getLogger("coordinator.notify")

WEBHOOK_TIMEOUT = 5.0

#: PowerShell that raises a real Windows toast without any third-party module.
TOAST_PS = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
    " ContentType=WindowsRuntime] > $null;"
    "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$n = $t.GetElementsByTagName('text');"
    "$n.Item(0).AppendChild($t.CreateTextNode('%(title)s')) > $null;"
    "$n.Item(1).AppendChild($t.CreateTextNode('%(body)s')) > $null;"
    "$toast = [Windows.UI.Notifications.ToastNotification]::new($t);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'PhoneCluster').Show($toast)"
)


def _ps_quote(text: str) -> str:
    return str(text).replace("'", "''").replace("\r", " ").replace("\n", " ")


def desktop_toast(title: str, body: str) -> str:
    """Blocking desktop notification.  Returns the mechanism that was used."""
    if sys.platform.startswith("win"):
        script = TOAST_PS % {"title": _ps_quote(title), "body": _ps_quote(body)}
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0:
                return "toast"
            log.debug("toast failed: %s", proc.stderr.decode("utf-8", "replace")[:300])
        except Exception:
            log.debug("toast could not be shown", exc_info=True)
    log.info("[notify] %s: %s", title, body)
    return "log"


class Notifier:
    """Fans a cluster event out to desktop / phones / webhook."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.log = logging.getLogger("coordinator.notify")

    # ------------------------------------------------------------- settings
    def settings(self) -> Dict[str, Any]:
        value = self.state.setting("notifications", {}) or {}
        return dict(value)

    def enabled_for(self, kind: str) -> bool:
        wanted = self.settings().get("on")
        if wanted is None:
            return True
        return str(kind) in [str(k) for k in wanted]

    # ---------------------------------------------------------------- fire
    async def fire(
        self,
        kind: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        title: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Deliver a notification through every enabled channel."""
        settings = self.settings()
        if not force and not self.enabled_for(kind):
            return {"sent": [], "skipped": "kind not enabled"}
        title = title or "PhoneCluster"
        sent: List[str] = []
        if settings.get("desktop"):
            try:
                mechanism = await asyncio.to_thread(desktop_toast, title, message)
                sent.append("desktop:%s" % mechanism)
            except Exception:
                self.log.debug("desktop notification failed", exc_info=True)
        if settings.get("phone"):
            count = self.push_to_phones(title, message)
            if count:
                sent.append("phone:%d" % count)
        url = settings.get("webhook_url")
        if url:
            ok = await self.webhook(str(url), kind, message, data or {})
            sent.append("webhook:%s" % ("ok" if ok else "failed"))
        event = self.state.event(
            "notify_sent",
            message,
            data={"kind": kind, "channels": sent, "data": data or {}},
        )
        return {"sent": sent, "event": event}

    def fire_soon(
        self,
        kind: str,
        message: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        title: Optional[str] = None,
    ) -> None:
        """Fire-and-forget wrapper usable from synchronous scheduler code."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop (tests / shutdown)
            return
        loop.create_task(self.fire(kind, message, data, title=title))

    # ------------------------------------------------------------ channels
    def push_to_phones(self, title: str, body: str) -> int:
        """Send a ``notify`` command to every online device with ``termux_api``."""
        count = 0
        for device in self.state.db.select(
            "devices", "WHERE removed=0 AND status='online'"
        ):
            caps = device.get("caps") or []
            if "termux_api" not in caps and device.get("platform") != "browser":
                continue
            self.state.send_command(
                device["id"], {"cmd": "notify", "title": title, "body": body}
            )
            count += 1
        return count

    async def webhook(self, url: str, kind: str, message: str, data: Dict[str, Any]) -> bool:
        import time

        import httpx

        payload = {"kind": kind, "message": message, "data": data, "ts": time.time()}
        try:
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
                response = await client.post(url, json=payload)
            if response.status_code >= 400:
                self.log.warning("webhook %s returned %s", url, response.status_code)
                return False
            return True
        except Exception as exc:
            self.log.warning("webhook %s failed: %s", url, exc)
            return False

    # -------------------------------------------------------------- direct
    def notify_device(self, device_id: str, title: str, body: str) -> bool:
        """``POST /api/devices/{id}/notify`` -- push straight to one device."""
        device = self.state.db.get("devices", device_id)
        if device is None or device.get("removed"):
            return False
        self.state.send_command(device_id, {"cmd": "notify", "title": title, "body": body})
        self.state.event(
            "notify_sent",
            "%s -> %s" % (title, device.get("name")),
            device_id=device_id,
            data={"title": title, "body": body},
        )
        return True
