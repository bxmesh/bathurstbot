"""Fills BathurstBot's fact pools in the background, slowly ([FactPool] in local/config.ini).

At most one Workers AI call per refill_interval_seconds, and only while a pool isn't full: the
topic with the fewest facts gets the next one. A fact over the length limit, or one that isn't a
single plain fact, is thrown away, and the next interval tries again. Nothing here ever makes a
reply wait: bathurstfact and meow take what's in the pool, or a static fact (local/factpool.py).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

from modules.service_plugins.base_service import BaseServicePlugin

# local/factpool.py, shared with the commands (one module, so one pool).
_LOCAL_DIR = str(Path(__file__).resolve().parent.parent)
if _LOCAL_DIR not in sys.path:
    sys.path.insert(0, _LOCAL_DIR)
import factpool  # noqa: E402


class FactPoolService(BaseServicePlugin):
    config_section = "FactPool"
    name = "factpool"
    description = "Keeps a small pool of AI-written facts for bathurstfact and meow"

    def __init__(self, bot: Any):
        super().__init__(bot)
        cfg = bot.config
        section = self.config_section
        self.data_dir = cfg.get(section, "data_dir", fallback=factpool.DEFAULT_DATA_DIR)
        self.pool_size = cfg.getint(section, "pool_size", fallback=10)
        self.interval = max(60.0, cfg.getfloat(section, "refill_interval_seconds", fallback=180.0))
        self.first_delay = max(0.0, cfg.getfloat(section, "first_refill_delay_seconds", fallback=30.0))
        self.pool = factpool.FactPool(
            cfg.get(section, "pool_file", fallback="/data/factpool.json"), self.pool_size
        )
        self.ai = factpool.WorkersAI(
            account_id_file=cfg.get(section, "account_id_file",
                                    fallback="/srv/secrets/workers_ai_account_id"),
            token_file=cfg.get(section, "token_file", fallback="/srv/secrets/workers_ai_token"),
            model=cfg.get(section, "model", fallback=factpool.DEFAULT_MODEL),
            timeout=cfg.getfloat(section, "request_timeout_seconds", fallback=60.0),
        )
        self._task: Optional[asyncio.Task] = None

    def fact_limit(self) -> int:
        """Half a scoped channel reply's budget, for the companion's name as the mesh sees it."""
        name = None
        meshcore = getattr(self.bot, "meshcore", None)
        info = getattr(meshcore, "self_info", None) if meshcore else None
        if isinstance(info, dict):
            name = info.get("name") or info.get("adv_name")
        if not name:
            name = self.bot.config.get("Bot", "bot_name", fallback="Bot")
        return factpool.fact_limit_for_name(name)

    async def start(self) -> None:
        self._running = True
        counts = ", ".join(f"{t} {self.pool.count(t)}" for t in factpool.TOPICS)
        self.logger.info(f"FactPool: started, {counts} of {self.pool_size} ready; "
                         f"one Workers AI call every {int(self.interval)} s at most while not full")
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
        self.logger.info("FactPool: stopped")

    async def refill_once(self) -> str:
        """One step: generate one fact for the neediest topic. Returns what happened."""
        topic = self.pool.neediest()
        if topic is None:
            return "full"
        if not self.ai.configured():
            return "no Workers AI secrets: answering from the fallback lists"
        limit = self.fact_limit()
        try:
            fact, why = await asyncio.to_thread(
                factpool.generate_fact, self.ai, self.data_dir, topic, limit
            )
        except factpool.WorkersAIError as e:
            return f"{topic}: Workers AI: {e}"
        if fact is None:
            return f"{topic}: {why}"
        if self.pool.add(topic, fact):
            return f"{topic}: added a fact ({self.pool.count(topic)} of {self.pool_size})"
        return f"{topic}: a duplicate, not added"

    async def _run(self) -> None:
        await asyncio.sleep(self.first_delay)
        last = ""
        while self._running:
            try:
                outcome = await self.refill_once()
            except Exception as e:  # never let the loop die; never log secrets
                outcome = f"refill failed: {type(e).__name__}"
            if outcome != "full" and outcome != last:
                self.logger.info(f"FactPool: {outcome}")
            elif outcome != "full":
                self.logger.debug(f"FactPool: {outcome}")
            last = outcome
            await asyncio.sleep(self.interval)
