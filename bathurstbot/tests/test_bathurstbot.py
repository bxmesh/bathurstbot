"""BathurstBot's tests: the config, bathurstfact, meow and the fact pool.

They run inside the built image, against meshcore-bot's own code (CI does this before pushing):

    docker run --rm --entrypoint python3 -v "$PWD/bathurstbot/tests:/tests:ro" <image> \
        -m unittest discover -s /tests -v

Workers AI is always faked: the HTTP layer is replaced, and any real network call fails the test.
"""

import asyncio
import configparser
import importlib
import json
import logging
import os
import random
import sys
import tempfile
import time
import types
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

APP = Path("/app")
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "local"))

import factpool  # noqa: E402
from modules.commands.base_command import BaseCommand  # noqa: E402
from modules.models import MeshMessage  # noqa: E402

CONFIG = APP / "bathurstbot" / "config.ini"
LOCAL_CONFIG = APP / "local" / "config.ini"
DATA = APP / "bathurstbot" / "data"


def load_config() -> configparser.ConfigParser:
    """The config as meshcore-bot reads it: the main file, then local/config.ini over it."""
    config = configparser.ConfigParser()
    config.read(CONFIG, encoding="utf-8")
    config.read(LOCAL_CONFIG, encoding="utf-8")
    return config


def load_local_command(stem: str):
    path = APP / "local" / "commands" / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"local_plugins_test.{stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeBot:
    """Enough of MeshCoreBot for a command to run: config, a logger, and a command manager that
    records what would have been sent."""

    def __init__(self, services=None):
        self.config = load_config()
        self.logger = logging.getLogger("bathurstbot-test")
        self.meshcore = None
        self.translator = None
        self.services = services or {}
        self.sent: list[str] = []

        async def send_response(message, content, skip_user_rate_limit=False, command_id=None):
            self.sent.append(content)
            return True

        self.command_manager = SimpleNamespace(monitor_channels=["#test"], send_response=send_response)


def channel_message(text: str, scope, channel: str = "#test") -> MeshMessage:
    return MeshMessage(content=text, sender_id="Tester", channel=channel, is_dm=False, reply_scope=scope)


def dm_message(text: str) -> MeshMessage:
    return MeshMessage(content=text, sender_id="Tester", is_dm=True)


def run(coro):
    return asyncio.run(coro)


class NoNetwork(unittest.TestCase):
    """Every test: a real HTTP request would fail it."""

    def setUp(self):
        patcher = mock.patch.object(urllib.request, "urlopen",
                                    side_effect=AssertionError("no real network calls in tests"))
        patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------------------------
# The config
# ---------------------------------------------------------------------------------------------

ENABLED = {
    # mesh tools
    "test", "ping", "multitest", "path", "trace", "prefix", "stats", "channels", "advert",
    # info
    "help", "cmd", "version", "contact", "schedule",
}
ADMIN_ONLY_NO_SWITCH = {"reload", "channelpause"}  # no enabled key upstream; admin-gated


def builtin_command_classes():
    classes = {}
    for path in sorted((APP / "modules" / "commands").glob("*_command.py")):
        module = importlib.import_module(f"modules.commands.{path.stem}")
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, BaseCommand) and obj is not BaseCommand \
                    and obj.__module__ == module.__name__:
                classes[obj.name] = obj
    return classes


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_only_test_channel_and_never_public(self):
        self.assertEqual(self.config.get("Channels", "monitor_channels").strip(), "#test")
        for key in self.config["Bot"]:
            self.assertFalse(key.startswith("i_understand_that_running_the_bot_on_the_public"),
                             "upstream's Public guard must stay on")

    def test_scopes_au_nsw_bhs_and_no_unscoped(self):
        scopes = [s.strip() for s in self.config.get("Channels", "flood_scopes").split(",")]
        self.assertEqual(scopes, ["#au", "#au-nsw", "#au-nsw-bhs"])
        self.assertNotIn("*", scopes)

    def test_tcp_placeholders_not_an_address(self):
        self.assertEqual(self.config.get("Connection", "connection_type"), "tcp")
        self.assertEqual(self.config.get("Connection", "hostname"), "@@COMPANION_HOST@@")
        self.assertEqual(self.config.get("Connection", "tcp_port"), "@@COMPANION_PORT@@")
        self.assertEqual(self.config.getint("Connection", "reconnect_max_retries"), 0)

    def test_exactly_the_wanted_builtins_enabled(self):
        classes = builtin_command_classes()
        self.assertTrue(ENABLED <= set(classes), ENABLED - set(classes))
        for name, cls in classes.items():
            section = BaseCommand._derive_config_section_name(SimpleNamespace(name=name))
            if name in ADMIN_ONLY_NO_SWITCH:
                continue
            self.assertTrue(self.config.has_option(section, "enabled"),
                            f"[{section}] must say enabled = true or false")
            self.assertEqual(self.config.getboolean(section, "enabled"), name in ENABLED,
                             f"{name}: enabled should be {name in ENABLED}")
            if name not in ENABLED:
                self.assertEqual(self.config.get(section, "channels", fallback=None), "",
                                 f"[{section}] disabled, so allowed in no channel")

    def test_catfact_off_so_meow_is_ours(self):
        self.assertFalse(self.config.getboolean("Catfact_Command", "enabled"))
        self.assertTrue(self.config.getboolean("Meow_Command", "enabled"))
        self.assertTrue(self.config.getboolean("Bathurstfact_Command", "enabled"))

    def test_multitest_aliases(self):
        aliases = [a.strip() for a in self.config.get("Multitest_Command", "aliases").split(",")]
        self.assertEqual(aliases, ["multi", "multitrace"])

    def test_admin_commands_have_no_admins(self):
        self.assertEqual(self.config.get("Admin_ACL", "admin_pubkeys").strip(), "")
        admin = {c.strip() for c in self.config.get("Admin_ACL", "admin_commands").split(",")}
        self.assertTrue(ADMIN_ONLY_NO_SWITCH | {"gwx"} <= admin)
        self.assertEqual(self.config.get("Gwx_Command", "channels").strip(), "")
        for name in ADMIN_ONLY_NO_SWITCH:
            section = BaseCommand._derive_config_section_name(SimpleNamespace(name=name))
            self.assertEqual(self.config.get(section, "channels"), "")

    def test_keywords_and_fixed_help(self):
        self.assertEqual(set(self.config["Keywords"]), {"test", "ping", "help"})
        help_text = self.config.get("Keywords", "help").strip('"')
        # fits a scoped channel reply from BathurstBot
        self.assertLessEqual(len(help_text.encode()), 160 - len("BathurstBot") - 2 - 10)
        words = set(help_text.replace("|", " ").split())
        for name in ENABLED - {"multitest", "help"}:
            self.assertIn(name, words)
        self.assertTrue({"mt", "bxfact", "meow"} <= words)
        self.assertNotIn("neighbors", words)

    def test_no_packet_capture_no_neighbour_discovery(self):
        self.assertFalse(self.config.getboolean("PacketCapture", "enabled"))
        self.assertFalse(self.config.getboolean("PacketCapture", "neighbors_enabled"))
        self.assertFalse(self.config.getboolean("PacketCapture", "mqtt_enabled"))
        self.assertFalse(self.config.getboolean("Neighbors_Command", "enabled"))

    def test_hop_limit(self):
        self.assertEqual(self.config.getint("Channels", "max_response_hops"), 12)

    def test_adverts_every_49_hours_none_at_start(self):
        self.assertEqual(self.config.getint("Bot", "advert_interval_hours"), 49)
        self.assertEqual(self.config.get("Bot", "startup_advert"), "false")

    def test_disabled_builtin_stays_silent(self):
        from modules.commands.catfact_command import CatfactCommand
        catfact = CatfactCommand(FakeBot())
        self.assertFalse(catfact.can_execute(channel_message("meow", "#au-nsw-bhs")))


# ---------------------------------------------------------------------------------------------
# The data
# ---------------------------------------------------------------------------------------------

class DataTest(unittest.TestCase):
    def test_passages_and_attribution(self):
        for topic, minimum in (("bathurstfact", 60), ("meow", 15)):
            self.assertGreaterEqual(len(factpool.load_passages(str(DATA), topic)), minimum)
            for path in Path(DATA, topic).glob("*.txt"):
                head = path.read_text(encoding="utf-8")[:1200]
                self.assertRegex(head, r"# Source: .*, (Wikipedia|Wikiquote)", path)
                self.assertIn("CC BY-SA 4.0", head, path)
                self.assertIn("oldid=", head, path)

    def test_cat_game_scene_is_a_meow_passage(self):
        path = Path(DATA, "meow", "super-troopers-cat-game.txt")
        head = path.read_text(encoding="utf-8")[:1200]
        self.assertIn("Wikiquote", head)
        self.assertIn("oldid=4017742", head)
        self.assertIn("CC BY-SA 4.0", head)
        scene = factpool.read_lines(path)
        self.assertEqual(len(scene), 1)
        self.assertIn('how about "Cat Game?"', scene[0])
        self.assertIn(scene[0], factpool.load_passages(str(DATA), "meow"))

    def test_fallbacks_short(self):
        for topic in factpool.TOPICS:
            facts = factpool.load_fallback(str(DATA), topic)
            self.assertGreaterEqual(len(facts), 15)
            for fact in facts:
                self.assertLessEqual(factpool.byte_len(fact), 60, fact)

    def test_quotes_attributed(self):
        text = Path(DATA, "quotes", "meow.txt").read_text(encoding="utf-8")
        self.assertIn("Wikiquote", text)
        self.assertIn("CC BY-SA 4.0", text)
        self.assertIn("oldid=4017742", text)
        quotes = factpool.read_lines(Path(DATA, "quotes", "meow.txt"))
        self.assertGreaterEqual(len(quotes), 30)
        for q in quotes:
            self.assertRegex(q, r"^[A-Z][A-Za-z' ]+: \S", q)


# ---------------------------------------------------------------------------------------------
# bathurstfact
# ---------------------------------------------------------------------------------------------

class PoolStub:
    def __init__(self, facts):
        self.pool = factpool.FactPool(os.path.join(tempfile.mkdtemp(), "pool.json"), size=10)
        for topic, items in facts.items():
            for item in items:
                self.pool.add(topic, item)


class BathurstfactTest(NoNetwork):
    def setUp(self):
        super().setUp()
        self.module = load_local_command("bathurstfact_command")

    def test_keywords(self):
        cmd = self.module.BathurstfactCommand(FakeBot())
        self.assertEqual(set(cmd.keywords), {"bathurstfact", "bxfact"})
        for word in ("bathurstfact", "bxfact"):
            self.assertTrue(cmd.should_execute(channel_message(word, "#au")))

    def test_answers_on_all_three_scopes(self):
        cmd = self.module.BathurstfactCommand(FakeBot())
        for scope in ("#au", "#au-nsw", "#au-nsw-bhs"):
            self.assertTrue(cmd.can_execute(channel_message("bathurstfact", scope)), scope)

    def test_limit_is_half_the_bots_maximum(self):
        bot = FakeBot()
        cmd = self.module.BathurstfactCommand(bot)
        message = channel_message("bathurstfact", "#au-nsw-bhs")
        self.assertEqual(cmd.get_max_message_length(message), 160 - len("BathurstBot") - 2 - 10)
        limit = cmd.get_max_message_length(message) // 2
        self.assertEqual(limit, factpool.fact_limit_for_name("BathurstBot"))

    def test_pooled_fact_first_and_too_long_skipped(self):
        long_fact = "x" * 69
        stub = PoolStub({"bathurstfact": [long_fact, "Bathurst was founded in 1815."]})
        bot = FakeBot(services={"factpool": stub})
        cmd = self.module.BathurstfactCommand(bot)
        run(cmd.execute(channel_message("bathurstfact", "#au-nsw-bhs")))
        self.assertEqual(bot.sent, ["Bathurst was founded in 1815."])
        self.assertEqual(stub.pool.facts["bathurstfact"], [long_fact])  # still there, for a DM

    def test_fallback_when_pool_empty(self):
        bot = FakeBot(services={"factpool": PoolStub({})})
        cmd = self.module.BathurstfactCommand(bot)
        run(cmd.execute(channel_message("bxfact", "#au")))
        self.assertEqual(len(bot.sent), 1)
        self.assertIn(bot.sent[0], factpool.load_fallback(str(DATA), "bathurstfact"))

    def test_fallback_without_the_service(self):
        bot = FakeBot(services={})
        cmd = self.module.BathurstfactCommand(bot)
        run(cmd.execute(channel_message("bathurstfact", "#au-nsw")))
        self.assertLessEqual(factpool.byte_len(bot.sent[0]), 68)


# ---------------------------------------------------------------------------------------------
# meow
# ---------------------------------------------------------------------------------------------

class MeowTest(NoNetwork):
    def setUp(self):
        super().setUp()
        self.module = load_local_command("meow_command")
        self.quotes = set(factpool.read_lines(Path(DATA, "quotes", "meow.txt")))

    def test_answers_on_au_nsw_bhs(self):
        bot = FakeBot()
        cmd = self.module.MeowCommand(bot)
        message = channel_message("meow", "#au-nsw-bhs")
        self.assertTrue(cmd.should_execute(message))
        self.assertTrue(cmd.can_execute(message))
        run(cmd.execute(message))
        self.assertEqual(len(bot.sent), 1)

    def test_silent_on_au_and_au_nsw(self):
        for scope in ("#au", "#au-nsw"):
            bot = FakeBot()
            cmd = self.module.MeowCommand(bot)
            message = channel_message("meow", scope)
            self.assertFalse(cmd.can_execute(message), scope)
            run(cmd.execute(message))
            self.assertEqual(bot.sent, [], scope)

    def test_silent_unscoped_and_in_dm(self):
        bot = FakeBot()
        cmd = self.module.MeowCommand(bot)
        for message in (channel_message("meow", None), channel_message("meow", ""), dm_message("meow")):
            self.assertFalse(cmd.can_execute(message))
            run(cmd.execute(message))
        self.assertEqual(bot.sent, [])

    def test_about_three_quotes_in_four(self):
        bot = FakeBot(services={"factpool": PoolStub({})})
        cmd = self.module.MeowCommand(bot)
        cmd.rng = random.Random(2026)
        replies = [cmd.choose_reply(68) for _ in range(4000)]
        share = sum(r in self.quotes for r in replies) / len(replies)
        self.assertGreater(share, 0.72)
        self.assertLess(share, 0.78)
        facts = set(factpool.load_fallback(str(DATA), "meow"))
        self.assertTrue(all(r in self.quotes or r in facts for r in replies))

    def test_ai_fact_path_uses_the_pool(self):
        stub = PoolStub({"meow": ["Pool fact one.", "Pool fact two."]})
        bot = FakeBot(services={"factpool": stub})
        cmd = self.module.MeowCommand(bot)
        cmd.quote_share = 0.0
        self.assertEqual(cmd.choose_reply(68), "Pool fact one.")

    def test_quote_length_filter(self):
        rng = random.Random(1)
        for limit in (20, 30, 40, 68):
            fitting = {q for q in self.quotes if factpool.byte_len(q) <= limit}
            seen = {factpool.pick_quote(str(DATA), "meow", limit, rng) for _ in range(500)}
            seen.discard(None)
            self.assertTrue(seen <= fitting, limit)
            if fitting:
                self.assertTrue(seen)
        self.assertIsNone(factpool.pick_quote(str(DATA), "meow", 5, rng))

    def test_no_quote_fits_falls_back_to_a_fact(self):
        bot = FakeBot(services={})
        cmd = self.module.MeowCommand(bot)
        cmd.quote_share = 1.0
        reply = cmd.choose_reply(12)
        self.assertNotIn(reply, self.quotes)
        self.assertLessEqual(factpool.byte_len(reply), 12)

    def test_whole_reply_within_half_the_maximum(self):
        bot = FakeBot(services={"factpool": PoolStub({})})
        cmd = self.module.MeowCommand(bot)
        cmd.rng = random.Random(7)
        message = channel_message("meow", "#au-nsw-bhs")
        limit = cmd.get_max_message_length(message) // 2
        for _ in range(300):
            run(cmd.execute(message))
        self.assertTrue(all(factpool.byte_len(r) <= limit for r in bot.sent))


# ---------------------------------------------------------------------------------------------
# The fact pool and Workers AI (faked)
# ---------------------------------------------------------------------------------------------

class FakeHTTP:
    """Stands in for the HTTP POST: records the request, returns a canned response."""

    def __init__(self, status=200, body=None, content="Bathurst was founded in 1815."):
        self.status = status
        self.body = body if body is not None else {
            "result": {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                       "choices": [{"index": 0, "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": content}}]},
            "success": True, "errors": [], "messages": [],
        }
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body), timeout))
        return self.status, json.dumps(self.body).encode()


class SecretsDir:
    def __init__(self, account="acct123", token="tok456"):
        self.dir = tempfile.mkdtemp()
        self.account = os.path.join(self.dir, "workers_ai_account_id")
        self.token = os.path.join(self.dir, "workers_ai_token")
        if account:
            Path(self.account).write_text(account + "\n")
        if token:
            Path(self.token).write_text(token + "\n")


class WorkersAITest(NoNetwork):
    def test_request_shape(self):
        s = SecretsDir()
        http = FakeHTTP()
        ai = factpool.WorkersAI(s.account, s.token, post=http)
        answer = ai.run(factpool.build_messages("bathurstfact", "A passage.", 68))
        self.assertEqual(answer, "Bathurst was founded in 1815.")
        url, headers, payload, _ = http.calls[0]
        self.assertEqual(
            url, "https://api.cloudflare.com/client/v4/accounts/acct123/ai/run/@cf/zai-org/glm-4.7-flash")
        self.assertEqual(headers["Authorization"], "Bearer tok456")
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        self.assertIn("A passage.", payload["messages"][1]["content"])
        self.assertIn("68 characters", payload["messages"][0]["content"])

    def test_errors_never_carry_secrets(self):
        s = SecretsDir()
        http = FakeHTTP(status=401, body={"success": False, "errors": [{"message": "Authentication error"}]})
        ai = factpool.WorkersAI(s.account, s.token, post=http)
        with self.assertRaises(factpool.WorkersAIError) as ctx:
            ai.run([{"role": "user", "content": "x"}])
        self.assertIn("401", str(ctx.exception))
        self.assertNotIn("tok456", str(ctx.exception))
        self.assertNotIn("acct123", str(ctx.exception))

        def boom(url, headers, body, timeout):
            raise OSError(f"cannot reach {url} with {headers}")
        ai = factpool.WorkersAI(s.account, s.token, post=boom)
        with self.assertRaises(factpool.WorkersAIError) as ctx:
            ai.run([{"role": "user", "content": "x"}])
        self.assertNotIn("tok456", str(ctx.exception))
        self.assertNotIn("acct123", str(ctx.exception))

    def test_missing_secrets(self):
        s = SecretsDir(token="")
        ai = factpool.WorkersAI(s.account, s.token, post=FakeHTTP())
        self.assertFalse(ai.configured())
        with self.assertRaises(factpool.WorkersAIError):
            ai.run([])

    def test_reads_only_the_final_answer(self):
        body = {"result": {"choices": [{"message": {"role": "assistant", "content": "The answer.",
                                                    "reasoning_content": "secret thoughts"}}]},
                "success": True}
        self.assertEqual(factpool.extract_answer(body), "The answer.")
        self.assertEqual(factpool.extract_answer({"result": {"response": "Old style."}}), "Old style.")
        with self.assertRaises(factpool.WorkersAIError):
            factpool.extract_answer({"result": {"choices": [{"message": {"content": None},
                                                             "finish_reason": "length"}]}})


class CleanAndGenerateTest(NoNetwork):
    def test_clean(self):
        self.assertEqual(factpool.clean_fact("  Fact: Bathurst is old.\n"), "Bathurst is old.")
        self.assertEqual(factpool.clean_fact('"Quoted fact."'), "Quoted fact.")
        self.assertEqual(factpool.clean_fact("<think>hmm</think> A fact."), "A fact.")
        self.assertIsNone(factpool.clean_fact("Here is a fact: x"))
        self.assertIsNone(factpool.clean_fact("<think>never closed"))
        self.assertIsNone(factpool.clean_fact(""))
        self.assertIsNone(factpool.clean_fact(None))

    def test_generate_rejects_over_limit(self):
        s = SecretsDir()
        ai = factpool.WorkersAI(s.account, s.token, post=FakeHTTP(content="y" * 69))
        fact, why = factpool.generate_fact(ai, str(DATA), "bathurstfact", 68)
        self.assertIsNone(fact)
        self.assertIn("over the 68-byte limit", why)

    def test_generate_ok_uses_one_passage(self):
        s = SecretsDir()
        http = FakeHTTP(content="Mount Panorama is a public road.")
        ai = factpool.WorkersAI(s.account, s.token, post=http)
        fact, why = factpool.generate_fact(ai, str(DATA), "bathurstfact", 68, random.Random(3))
        self.assertEqual((fact, why), ("Mount Panorama is a public road.", "ok"))
        user = http.calls[0][2]["messages"][1]["content"]
        passages = factpool.load_passages(str(DATA), "bathurstfact")
        self.assertEqual(sum(p in user for p in passages), 1)


class FactPoolTest(unittest.TestCase):
    def test_persist_cap_dedupe(self):
        path = os.path.join(tempfile.mkdtemp(), "sub", "pool.json")
        pool = factpool.FactPool(path, size=2)
        self.assertEqual(pool.neediest(), "bathurstfact")
        self.assertTrue(pool.add("meow", "One."))
        self.assertFalse(pool.add("meow", "One."))
        self.assertTrue(pool.add("meow", "Two."))
        self.assertFalse(pool.add("meow", "Three."))
        self.assertEqual(pool.neediest(), "bathurstfact")
        again = factpool.FactPool(path, size=2)
        self.assertEqual(again.facts["meow"], ["One.", "Two."])
        self.assertEqual(again.pop("meow", 68), "One.")
        self.assertEqual(factpool.FactPool(path, size=2).facts["meow"], ["Two."])

    def test_corrupt_file_starts_empty(self):
        path = os.path.join(tempfile.mkdtemp(), "pool.json")
        Path(path).write_text("{not json")
        self.assertEqual(factpool.FactPool(path).facts, {"bathurstfact": [], "meow": []})

    def test_truncate_bytes(self):
        self.assertEqual(factpool.truncate_bytes("one two three", 9), "one two")
        self.assertLessEqual(factpool.byte_len(factpool.truncate_bytes("é" * 10, 5)), 5)


class ServiceTest(NoNetwork):
    def make_service(self, secrets: SecretsDir, http: FakeHTTP):
        sys.path.insert(0, str(APP / "local" / "service_plugins"))
        from factpool_service import FactPoolService
        bot = FakeBot()
        tmp = tempfile.mkdtemp()
        bot.config.set("FactPool", "pool_file", os.path.join(tmp, "pool.json"))
        bot.config.set("FactPool", "account_id_file", secrets.account)
        bot.config.set("FactPool", "token_file", secrets.token)
        service = FactPoolService(bot)
        service.ai._post = http
        return service

    def test_refill_once_fills_the_neediest(self):
        service = self.make_service(SecretsDir(), FakeHTTP(content="Bathurst is on the Macquarie."))
        self.assertIn("added", run(service.refill_once()))
        self.assertEqual(service.pool.facts["bathurstfact"], ["Bathurst is on the Macquarie."])
        self.assertEqual(service.fact_limit(), 68)

    def test_refill_without_secrets_does_nothing(self):
        http = FakeHTTP()
        service = self.make_service(SecretsDir(account="", token=""), http)
        self.assertIn("fallback", run(service.refill_once()))
        self.assertEqual(http.calls, [])

    def test_refill_reports_ai_errors(self):
        service = self.make_service(SecretsDir(), FakeHTTP(status=500, body={"success": False, "errors": []}))
        self.assertIn("Workers AI: HTTP 500", run(service.refill_once()))

    def test_config_values(self):
        service = self.make_service(SecretsDir(), FakeHTTP())
        self.assertEqual(service.pool_size, 10)
        self.assertEqual(service.interval, 180)
        self.assertEqual(service.ai.model, "@cf/zai-org/glm-4.7-flash")


# ---------------------------------------------------------------------------------------------
# LinkWatch: the keepalive, the skipped-message warning and the liveness file
# ---------------------------------------------------------------------------------------------

class FakeCommands:
    def __init__(self, answer=None):
        self.calls = 0
        self.answer = answer

    async def get_time(self):
        self.calls += 1
        return self.answer


class FakeMeshCore:
    def __init__(self, answer=None, connected=True):
        self.commands = FakeCommands(answer)
        self.is_connected = connected
        self.subscriptions = []

    def subscribe(self, event_type, callback):
        self.subscriptions.append((event_type, callback))


class LinkWatchTest(unittest.TestCase):
    def make_service(self, connected_ago: float = 10.0):
        sys.path.insert(0, str(APP / "local" / "service_plugins"))
        from linkwatch_service import LinkWatchService
        from meshcore import EventType
        from meshcore.events import Event
        from modules.message_handler import MessageHandler

        bot = FakeBot()
        bot.config.set("LinkWatch", "alive_file", os.path.join(tempfile.mkdtemp(), "alive"))
        bot.meshcore = FakeMeshCore(Event(EventType.CURRENT_TIME, {"time": 0}))
        bot.connected = True
        bot.connection_time = time.time() - connected_ago
        # meshcore-bot's own check, bound to this bot: the verdict LinkWatch reports is upstream's.
        holder = SimpleNamespace(bot=bot)
        bot.message_handler = SimpleNamespace(
            _is_old_cached_message=types.MethodType(MessageHandler._is_old_cached_message, holder)
        )
        bot.channel_manager = SimpleNamespace(get_channel_name={0: "Public", 1: "#test"}.get)
        return LinkWatchService(bot), bot

    def test_upstream_still_has_what_linkwatch_uses(self):
        from modules.message_handler import MessageHandler
        from modules.service_plugins.base_service import BaseServicePlugin
        self.assertTrue(callable(getattr(MessageHandler, "_is_old_cached_message", None)))
        self.assertTrue(callable(getattr(BaseServicePlugin, "on_transport_reconnected", None)))

    def test_config_values(self):
        service, _ = self.make_service()
        config = load_config()
        self.assertTrue(config.getboolean("LinkWatch", "enabled"))
        self.assertEqual(service.keepalive, 60)
        self.assertEqual(service.stale_after, 600)
        # Under openHop's 120 s idle timeout, with room for one missed answer.
        self.assertLess(config.getfloat("LinkWatch", "keepalive_seconds"), 120 / 2 + 1)
        self.assertEqual(config.get("LinkWatch", "alive_file"), "/tmp/bathurstbot-alive")

    def test_a_slow_clock_is_named(self):
        service, _ = self.make_service(connected_ago=10)
        with self.assertLogs("bathurstbot-test", level="WARNING") as logs:
            warning = service.explain_skip({
                "channel_idx": 1, "text": "🐝 Zindello TD: meow",
                "sender_timestamp": int(time.time() - 340),
            })
        self.assertIn("#test message from 🐝 Zindello TD gets no reply", warning)
        self.assertIn("5 m 4", warning)  # 340 s, give or take a second
        self.assertIn("clock needs setting", logs.output[0])

    def test_a_current_message_is_not_flagged(self):
        service, _ = self.make_service(connected_ago=10)
        self.assertIsNone(service.explain_skip({
            "channel_idx": 1, "text": "Tester: t", "sender_timestamp": int(time.time() - 2),
        }))

    def test_unmonitored_channels_are_left_alone(self):
        service, _ = self.make_service(connected_ago=10)
        self.assertIsNone(service.explain_skip({
            "channel_idx": 0, "text": "Tester: hi", "sender_timestamp": int(time.time() - 3600),
        }))

    def test_keepalive_asks_for_the_clock(self):
        service, bot = self.make_service()
        self.assertTrue(run(service.keepalive_once()))
        self.assertEqual(bot.meshcore.commands.calls, 1)

    def test_no_keepalive_while_reconnecting(self):
        service, bot = self.make_service()
        bot._transport_reconnect_in_progress = True
        self.assertFalse(run(service.keepalive_once()))
        bot._transport_reconnect_in_progress = False
        bot.meshcore.is_connected = False
        self.assertFalse(run(service.keepalive_once()))
        self.assertEqual(bot.meshcore.commands.calls, 0)

    def test_subscribes_once_per_meshcore_object(self):
        service, bot = self.make_service()
        service._subscribe()
        service._subscribe()
        self.assertEqual(len(bot.meshcore.subscriptions), 2)  # all events, and channel messages
        bot.meshcore = FakeMeshCore()  # what a reconnect does
        run(service.on_transport_reconnected())
        self.assertEqual(len(bot.meshcore.subscriptions), 2)

    def test_liveness_file_and_stale_warning(self):
        service, _ = self.make_service()
        service.on_any_event(None)
        self.assertTrue(os.path.exists(service.alive_file))
        self.assertFalse(service.check_stale())
        service._last_event = time.time() - 700
        with self.assertLogs("bathurstbot-test", level="WARNING"):
            self.assertTrue(service.check_stale())
        self.assertTrue(service.check_stale())  # warned once only
        with self.assertLogs("bathurstbot-test", level="INFO") as logs:
            service.on_any_event(None)
        self.assertIn("talking again", logs.output[0])


if __name__ == "__main__":
    unittest.main()
