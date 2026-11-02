"""bathurstfact (bxfact): one short fact about Bathurst NSW, Mount Panorama or the Bathurst 1000.

The fact comes ready-made from the pool the FactPool service keeps (written by Workers AI from the
Wikipedia passages in data/bathurstfact/), or from the static list when the pool is empty. It is at
most half the bot's maximum message length for this reply.
"""

import sys
from pathlib import Path

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage

_LOCAL_DIR = str(Path(__file__).resolve().parent.parent)
if _LOCAL_DIR not in sys.path:
    sys.path.insert(0, _LOCAL_DIR)
import factpool  # noqa: E402


class BathurstfactCommand(BaseCommand):
    name = "bathurstfact"
    keywords = ["bathurstfact", "bxfact"]
    description = "A short fact about Bathurst, Mount Panorama or the Bathurst 1000"
    category = "fun"
    short_description = "A short fact about Bathurst, Mount Panorama or the Bathurst 1000"
    usage = "bathurstfact"
    examples = ["bathurstfact", "bxfact"]

    topic = "bathurstfact"

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value("Bathurstfact_Command", "enabled", fallback=True, value_type="bool")

    def get_help_text(self) -> str:
        return "bathurstfact (or bxfact): a short fact about Bathurst, Mount Panorama or the Bathurst 1000"

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.enabled:
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    async def execute(self, message: MeshMessage) -> bool:
        limit = self.get_max_message_length(message) // 2
        return await self.send_response(message, factpool.take_fact(self.bot, self.topic, limit))
