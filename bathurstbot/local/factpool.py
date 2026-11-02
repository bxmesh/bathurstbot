"""BathurstBot's facts: the pools of AI-written facts, and what bathurstfact and meow answer.

Shared by the service plugin that fills the pools (service_plugins/factpool_service.py) and the
two commands that answer from them (commands/bathurstfact_command.py, commands/meow_command.py).
It lives outside commands/ and service_plugins/ so that meshcore-bot doesn't load it as a plugin.

How a fact is made: a random passage from the topic's bundled Wikipedia text (data/<topic>/) goes
to Workers AI, which is asked for one fact from that passage only, within the length limit, with
no preamble. A fact over the limit is rejected. Facts wait in a small pool per topic, saved on the
bot's volume, so a reply never waits for the AI. When a pool is empty, or the AI is down, the
topic's static list (data/fallback/<topic>.txt) answers instead.

meow also has quotes (data/quotes/meow.txt): real lines from the film, bundled verbatim from
Wikiquote and never written by the AI. The meow command answers with one of those most of the time.

Python's standard library only (and meshcore-bot's own modules), so the image installs nothing.
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

# The two topics: what each is about, for the prompt.
TOPICS: dict[str, str] = {
    "bathurstfact": "Bathurst, New South Wales: the city, its history, the Mount Panorama Circuit "
    "and the Bathurst 1000 motor race",
    "meow": "the 2001 comedy film Super Troopers, made by Broken Lizard, including its "
    "\"meow\" game",
}

DEFAULT_DATA_DIR = "/app/bathurstbot/data"
DEFAULT_MODEL = "@cf/zai-org/glm-4.7-flash"
API_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"

# meshcore-bot's limits (modules/models.py): a channel message is "<name>: <body>" in a 160-byte
# block, and a regional (scoped) flood costs another 10 bytes. Kept here too so the pool can work
# out its target length without a message in hand; the commands use the bot's own
# get_max_message_length(message).
CHANNEL_BLOCK_BYTES = 160
REGIONAL_SCOPE_OVERHEAD_BYTES = 10


def byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def fact_limit_for_name(bot_name: str) -> int:
    """Half the body budget of a scoped channel reply from bot_name, in UTF-8 bytes.

    Every channel reply BathurstBot makes is scoped (flood_scopes has no '*'), so this is the
    tightest budget a fact has to fit; a DM's is larger.
    """
    body = CHANNEL_BLOCK_BYTES - byte_len(bot_name) - 2 - REGIONAL_SCOPE_OVERHEAD_BYTES
    return max(body, 0) // 2


def read_lines(path: Path) -> list[str]:
    """Non-empty lines of a data file, without its # comments."""
    lines = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def load_passages(data_dir: str, topic: str) -> list[str]:
    """Every passage of the topic: one per line of data/<topic>/*.txt."""
    passages: list[str] = []
    for path in sorted(Path(data_dir, topic).glob("*.txt")):
        passages.extend(read_lines(path))
    return passages


_fallback_cache: dict[tuple[str, str], list[str]] = {}


def load_fallback(data_dir: str, topic: str) -> list[str]:
    key = (data_dir, topic)
    if key not in _fallback_cache:
        path = Path(data_dir, "fallback", f"{topic}.txt")
        _fallback_cache[key] = read_lines(path) if path.is_file() else []
    return _fallback_cache[key]


def truncate_bytes(text: str, max_bytes: int) -> str:
    """Cut text to max_bytes of UTF-8 at a word boundary, never mid-character."""
    if byte_len(text) <= max_bytes:
        return text
    cut = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:-")


def fallback_fact(data_dir: str, topic: str, max_bytes: int, rng: random.Random | None = None) -> str:
    """A random static fact that fits max_bytes (cut to fit if none does)."""
    rng = rng or random
    facts = load_fallback(data_dir, topic)
    fitting = [f for f in facts if byte_len(f) <= max_bytes]
    if fitting:
        return rng.choice(fitting)
    if facts:
        return truncate_bytes(min(facts, key=byte_len), max_bytes)
    return ""


def pick_quote(data_dir: str, topic: str, max_bytes: int, rng: random.Random | None = None) -> Optional[str]:
    """A random bundled quote (data/quotes/<topic>.txt) that fits max_bytes, or None.

    Quotes are sent exactly as stored, so one that's too long is skipped, never cut.
    """
    rng = rng or random
    path = Path(data_dir, "quotes", f"{topic}.txt")
    quotes = read_lines(path) if path.is_file() else []
    fitting = [q for q in quotes if byte_len(q) <= max_bytes]
    return rng.choice(fitting) if fitting else None


# ---------------------------------------------------------------------------------------------
# The prompt, and cleaning what comes back
# ---------------------------------------------------------------------------------------------

def build_messages(topic: str, passage: str, limit: int) -> list[dict[str, str]]:
    subject = TOPICS[topic]
    system = (
        "You write one short fact for a chat bot on a low-bandwidth radio mesh. "
        "Use only the passage you are given: nothing from elsewhere, nothing made up. "
        f"Reply with the fact alone, in plain English, at most {limit} characters: no preamble, "
        "no label, no quotation marks around it, no emoji, no hashtags, one sentence."
    )
    user = (
        f"Topic: {subject}.\n\n"
        f"Passage:\n{passage}\n\n"
        f"Write one interesting fact taken from this passage only, at most {limit} characters."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_PREFIX = re.compile(r"^(fact|fun fact|did you know)\s*[:\-–]\s*", re.I)


def clean_fact(text: Optional[str]) -> Optional[str]:
    """The fact from the model's answer, or None if there isn't a usable one.

    Drops any reasoning block that leaks into the answer, a "Fact:" label, quotation marks around
    the whole answer, and spreads over lines; refuses anything that still looks like more than a
    single fact.
    """
    if not text or not isinstance(text, str):
        return None
    text = _THINK.sub("", text)
    if "<think>" in text.lower() or "</think>" in text.lower():
        return None
    text = " ".join(text.split())
    text = _PREFIX.sub("", text)
    if len(text) >= 2 and text[0] in "\"'“‘" and text[-1] in "\"'”’":
        text = text[1:-1].strip()
    if not text or "\n" in text:
        return None
    lowered = text.lower()
    if lowered.startswith(("here is", "here's", "sure", "okay", "i ", "as an ai")):
        return None
    return text


# ---------------------------------------------------------------------------------------------
# Workers AI
# ---------------------------------------------------------------------------------------------

class WorkersAIError(Exception):
    """A failed call; the message never carries the token or the account ID."""


def read_secret(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


Post = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def _urllib_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class WorkersAI:
    """Cloudflare Workers AI's REST API, for one text-generation model.

    POST /client/v4/accounts/{account_id}/ai/run/{model} with the token as Bearer. GLM-4.7-Flash
    reasons by default; chat_template_kwargs.enable_thinking=false turns that off (its input
    schema), so the answer comes quickly and the token budget goes on the fact. Only the final
    answer (choices[0].message.content) is read, never any reasoning.
    """

    def __init__(self, account_id_file: str, token_file: str, model: str = DEFAULT_MODEL,
                 timeout: float = 60.0, post: Post | None = None):
        self.account_id_file = account_id_file
        self.token_file = token_file
        self.model = model
        self.timeout = timeout
        self._post = post or _urllib_post

    def configured(self) -> bool:
        return bool(read_secret(self.account_id_file) and read_secret(self.token_file))

    def run(self, messages: list[dict[str, str]], max_tokens: int = 400) -> str:
        account_id = read_secret(self.account_id_file)
        token = read_secret(self.token_file)
        if not account_id or not token:
            raise WorkersAIError("no Workers AI account ID or token in the secrets")
        payload = {
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "temperature": 0.7,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        url = API_URL.format(account_id=account_id, model=self.model)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            status, raw = self._post(url, headers, json.dumps(payload).encode("utf-8"), self.timeout)
        except Exception as e:  # network errors: report the kind, never the URL or headers
            raise WorkersAIError(f"request failed: {type(e).__name__}") from None
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise WorkersAIError(f"HTTP {status}: not JSON") from None
        if status != 200 or (isinstance(body, dict) and body.get("success") is False):
            errors = body.get("errors") if isinstance(body, dict) else None
            detail = "; ".join(str(e.get("message", ""))[:120] for e in errors or [] if isinstance(e, dict))
            raise WorkersAIError(f"HTTP {status}: {detail or 'error'}")
        return extract_answer(body)


def extract_answer(body: Any) -> str:
    """The final answer from a Workers AI response, without any reasoning.

    The REST API wraps the model's output in {"result": ..., "success": true}. GLM-4.7-Flash's
    output is OpenAI-style (choices[0].message.content); older Workers AI models answer in
    result.response, read as a fallback.
    """
    result = body.get("result", body) if isinstance(body, dict) else None
    if not isinstance(result, dict):
        raise WorkersAIError("no result in the response")
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
        raise WorkersAIError("no content in the answer (finish_reason: "
                             f"{choices[0].get('finish_reason') if isinstance(choices[0], dict) else '?'})")
    if isinstance(result.get("response"), str):
        return result["response"]
    raise WorkersAIError("no answer in the response")


# ---------------------------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------------------------

class FactPool:
    """A few ready facts per topic, saved as JSON on the bot's volume."""

    def __init__(self, path: str, size: int = 10):
        self.path = path
        self.size = size
        self.facts: dict[str, list[str]] = {t: [] for t in TOPICS}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            stored = data.get("facts", {}) if isinstance(data, dict) else {}
            for topic in TOPICS:
                items = stored.get(topic, [])
                self.facts[topic] = [s for s in items if isinstance(s, str) and s][: self.size]
        except (OSError, ValueError):
            pass  # no pool yet, or an unreadable one: start empty

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".factpool-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "facts": self.facts}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def count(self, topic: str) -> int:
        return len(self.facts[topic])

    def neediest(self) -> Optional[str]:
        """The topic with the fewest facts, if any pool isn't full."""
        short = [t for t in TOPICS if self.count(t) < self.size]
        return min(short, key=self.count) if short else None

    def add(self, topic: str, fact: str) -> bool:
        if self.count(topic) >= self.size or fact in self.facts[topic]:
            return False
        self.facts[topic].append(fact)
        self.save()
        return True

    def pop(self, topic: str, max_bytes: int) -> Optional[str]:
        """The oldest fact that fits max_bytes, removed from the pool."""
        for i, fact in enumerate(self.facts[topic]):
            if byte_len(fact) <= max_bytes:
                del self.facts[topic][i]
                try:
                    self.save()
                except OSError:
                    pass  # the reply matters more than the file; it's saved again next change
                return fact
        return None


def generate_fact(ai: WorkersAI, data_dir: str, topic: str, limit: int,
                  rng: random.Random | None = None) -> tuple[Optional[str], str]:
    """One try at a fact: (the fact or None, why not / what happened).

    Raises WorkersAIError when the call itself fails.
    """
    rng = rng or random
    passages = load_passages(data_dir, topic)
    if not passages:
        return None, "no passages"
    passage = rng.choice(passages)
    answer = ai.run(build_messages(topic, passage, limit))
    fact = clean_fact(answer)
    if fact is None:
        return None, "rejected: not a single plain fact"
    if byte_len(fact) > limit:
        return None, f"rejected: {byte_len(fact)} bytes, over the {limit}-byte limit"
    return fact, "ok"


def data_dir_for(bot: Any) -> str:
    config = getattr(bot, "config", None)
    if config is not None and config.has_section("FactPool"):
        return config.get("FactPool", "data_dir", fallback=DEFAULT_DATA_DIR)
    return DEFAULT_DATA_DIR


def take_fact(bot: Any, topic: str, max_bytes: int) -> str:
    """A fact for bathurstfact or meow: a pooled one if one fits, else a static one."""
    service = None
    services = getattr(bot, "services", None)
    if isinstance(services, dict):
        service = services.get("factpool")
    pool = getattr(service, "pool", None)
    if pool is not None:
        fact = pool.pop(topic, max_bytes)
        if fact:
            return fact
    return fallback_fact(data_dir_for(bot), topic, max_bytes)
