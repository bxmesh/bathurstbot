"""meow: a Super Troopers (2001) quote most of the time, otherwise a short fact about the film.

- Only on the region scope au-nsw-bhs ([Meow_Command] scopes, local/config.ini): a meow sent with
  au or au-nsw (which the bot otherwise answers), and a DM (which has no scope), get no reply.
  meshcore-bot v1.1.0 has no per-command scope setting, so this command checks the scope the
  message came in on (message.reply_scope) itself.
- About 3 times in 4 ([Meow_Command] quote_share) it sends a real line from the film, taken
  verbatim from Wikiquote (data/quotes/meow.txt), picked at random from those that fit; the AI
  never writes or recalls quotes.
- Otherwise, or when no quote fits, a fact: ready-made from the pool the FactPool service keeps
  (written by Workers AI from the Wikipedia passages in data/meow/), or from the static list.

Either way the reply is at most half the bot's maximum message length for it. The built-in
catfact, which also answers meow, is disabled in config.ini.
"""

import random
import sys
from pathlib import Path

from modules.commands.base_command import BaseCommand
from modules.flood_scope import normalize_scope_name
from modules.models import MeshMessage

_LOCAL_DIR = str(Path(__file__).resolve().parent.parent)
if _LOCAL_DIR not in sys.path:
    sys.path.insert(0, _LOCAL_DIR)
import factpool  # noqa: E402


class MeowCommand(BaseCommand):
    name = "meow"
    keywords = ["meow"]
    description = "A Super Troopers quote, or a short fact about the film"
    category = "fun"
    short_description = "A Super Troopers quote, or a short fact about the film"
    usage = "meow"
    examples = ["meow"]

    topic = "meow"

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = self.get_config_value("Meow_Command", "enabled", fallback=True, value_type="bool")
        raw_scopes = self.get_config_value("Meow_Command", "scopes", fallback="#au-nsw-bhs", value_type="str")
        self.scopes = {
            normalize_scope_name(s.strip()).lower() for s in str(raw_scopes).split(",") if s.strip()
        }
        share = self.get_config_value("Meow_Command", "quote_share", fallback=0.75, value_type="float")
        self.quote_share = min(max(float(share), 0.0), 1.0)
        self.rng = random.Random()

    def get_help_text(self) -> str:
        return "meow: a Super Troopers quote, or a short fact about the film"

    def scope_allowed(self, message: MeshMessage) -> bool:
        """True only for a channel message that came in on one of the allowed scopes."""
        if message.is_dm or not message.reply_scope:
            return False
        return normalize_scope_name(message.reply_scope.strip()).lower() in self.scopes

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.enabled or not self.scope_allowed(message):
            return False  # silent: no reply, and no "can't" message
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    def choose_reply(self, max_bytes: int) -> str:
        if self.rng.random() < self.quote_share:
            quote = factpool.pick_quote(factpool.data_dir_for(self.bot), self.topic, max_bytes, self.rng)
            if quote:
                return quote
        return factpool.take_fact(self.bot, self.topic, max_bytes)

    async def execute(self, message: MeshMessage) -> bool:
        if not self.scope_allowed(message):  # checked again: execute is the last word
            return True
        limit = self.get_max_message_length(message) // 2
        return await self.send_response(message, self.choose_reply(limit))
