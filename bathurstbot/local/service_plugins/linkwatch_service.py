"""Watches BathurstBot's link to its companion ([LinkWatch] in local/config.ini).

Three jobs, none of which sends anything over the air:

1. **Keepalive.** Asks the companion for its clock (get_time, a host-to-companion frame) every
   keepalive_seconds while connected. openHop's frame server closes a client that has sent it
   nothing for its tcp_timeout, which is 120 s for a companion added from its web page without a
   restart (openhop_repeater repeater/main.py, add_companion_from_config). meshcore-bot on its own
   sends nothing between its radio probes, at least 300 s apart, so it was cut off every 2-3
   minutes.

2. **Says why a message gets no reply.** meshcore-bot drops a channel message whose sender's
   clock is more than 5 s before the bot's last (re)connect, taking it for one the companion
   cached before the connection, and logs that only at DEBUG (modules/message_handler.py,
   _is_old_cached_message). A sender whose clock runs slow then gets no reply and nothing in the
   log says why. This logs a warning naming the sender and how far behind its clock is.

3. **Liveness.** Touches a file each time the companion sends anything (adverts, packets, its
   heartbeat every 15 s, the keepalive's answer). If nothing has come for stale_after_seconds it
   logs a warning; the pod's liveness probe checks the file's age and restarts the container
   when it's older than that (deploy/templates/bathurstbot.yaml).
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import Any, Optional

from meshcore import EventType

from modules.service_plugins.base_service import BaseServicePlugin


def clock(ts: float) -> str:
    """A local wall-clock time, for people."""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def duration(seconds: float) -> str:
    """'5 m 40 s', '2 h 3 m', '12 s'."""
    s = int(round(abs(seconds)))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} m {s % 60} s"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60} m"
    return f"{s // 86400} d {(s % 86400) // 3600} h"


class LinkWatchService(BaseServicePlugin):
    config_section = "LinkWatch"
    name = "linkwatch"
    description = "Keeps the companion link alive, says why a message is skipped, feeds liveness"

    def __init__(self, bot: Any):
        super().__init__(bot)
        cfg = bot.config
        section = self.config_section
        self.keepalive = max(15.0, cfg.getfloat(section, "keepalive_seconds", fallback=60.0))
        self.alive_file = cfg.get(section, "alive_file", fallback="/tmp/bathurstbot-alive")
        self.stale_after = max(60.0, cfg.getfloat(section, "stale_after_seconds", fallback=600.0))
        self.touch_every = 30.0
        self._last_event = time.time()
        self._last_touch = 0.0
        self._stale_warned = False
        self._subscribed_to: Optional[int] = None
        self._task: Optional[asyncio.Task] = None

    # -- lifecycle ------------------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._subscribe()
        self.touch(force=True)
        self.logger.info(
            f"LinkWatch: started; asks the companion for its clock every {int(self.keepalive)} s, "
            f"and the liveness probe fails after {duration(self.stale_after)} with nothing from it"
        )
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.logger.info("LinkWatch: stopped")

    async def on_transport_reconnected(self) -> None:
        """meshcore-bot builds a new MeshCore object on each reconnect: subscribe to it."""
        self._subscribe()

    def _subscribe(self) -> None:
        meshcore = getattr(self.bot, "meshcore", None)
        if meshcore is None or id(meshcore) == self._subscribed_to:
            return
        # Sync callbacks: the dispatcher calls them inline, and both are cheap.
        meshcore.subscribe(None, self.on_any_event)
        meshcore.subscribe(EventType.CHANNEL_MSG_RECV, self.on_channel_message)
        self._subscribed_to = id(meshcore)

    # -- liveness -------------------------------------------------------------------------------

    def on_any_event(self, event: Any) -> None:
        self._last_event = time.time()
        if self._stale_warned:
            self._stale_warned = False
            self.logger.info("LinkWatch: the companion is talking again")
        self.touch()

    def touch(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_touch < self.touch_every:
            return
        try:
            with open(self.alive_file, "a"):
                pass
            os.utime(self.alive_file, (now, now))
            self._last_touch = now
        except OSError as e:
            self.logger.warning(f"LinkWatch: can't touch {self.alive_file}: {e}")

    def check_stale(self) -> bool:
        """Warn once when nothing has come from the companion for stale_after seconds."""
        quiet = time.time() - self._last_event
        if quiet < self.stale_after:
            return False
        if not self._stale_warned:
            self._stale_warned = True
            self.logger.warning(
                f"LinkWatch: nothing from the companion for {duration(quiet)} "
                f"(the last at {clock(self._last_event)}); the liveness probe will restart the bot"
            )
        return True

    # -- keepalive ------------------------------------------------------------------------------

    def link_up(self) -> bool:
        meshcore = getattr(self.bot, "meshcore", None)
        return bool(
            meshcore is not None
            and getattr(meshcore, "is_connected", False)
            and getattr(self.bot, "connected", False)
            and not getattr(self.bot, "_transport_reconnect_in_progress", False)
        )

    async def keepalive_once(self) -> bool:
        """One get_time to the companion; True if it answered. Never raises."""
        if not self.link_up():
            return False
        try:
            result = await asyncio.wait_for(self.bot.meshcore.commands.get_time(), timeout=10.0)
        except Exception as e:  # the bot's own radio probe deals with a dead link
            self.logger.debug(f"LinkWatch: keepalive got no answer: {type(e).__name__}")
            return False
        return getattr(result, "type", None) == EventType.CURRENT_TIME

    async def _run(self) -> None:
        while self._running:
            await asyncio.sleep(self.keepalive)
            try:
                self._subscribe()
                await self.keepalive_once()
                self.check_stale()
            except Exception as e:  # never let the loop die
                self.logger.warning(f"LinkWatch: {type(e).__name__}: {e}")

    # -- skipped messages -----------------------------------------------------------------------

    def on_channel_message(self, event: Any) -> None:
        try:
            self.explain_skip(getattr(event, "payload", None))
        except Exception as e:
            self.logger.debug(f"LinkWatch: couldn't check a channel message: {e}")

    def explain_skip(self, payload: Any) -> Optional[str]:
        """If meshcore-bot will drop this message as cached from before the connection, say so.

        Uses meshcore-bot's own check, so the verdict is the one it reaches. Returns the warning,
        or None. Only for the channels the bot answers on.
        """
        if not isinstance(payload, dict):
            return None
        handler = getattr(self.bot, "message_handler", None)
        is_old = getattr(handler, "_is_old_cached_message", None)
        connected_at = getattr(self.bot, "connection_time", None)
        if is_old is None or connected_at is None:
            return None
        channel = None
        channels = getattr(self.bot, "channel_manager", None)
        if channels is not None:
            channel = channels.get_channel_name(payload.get("channel_idx", 0))
        monitored = getattr(getattr(self.bot, "command_manager", None), "monitor_channels", [])
        if channel not in monitored:
            return None
        sent_at = payload.get("sender_timestamp")
        if not is_old(sent_at):
            return None
        text = payload.get("text", "")
        sender = text.split(":", 1)[0].strip() if ":" in text else "someone"
        behind = time.time() - float(sent_at)
        warning = (
            f"LinkWatch: {channel} message from {sender} gets no reply: its clock says "
            f"{clock(float(sent_at))}, {duration(behind)} behind ours and before the bot's last "
            f"connection to the companion at {clock(connected_at)}, so meshcore-bot takes it for "
            f"an old message cached before the connection. That device's clock needs setting."
        )
        self.logger.warning(warning)
        return warning
