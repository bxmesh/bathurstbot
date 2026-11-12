# BathurstBot

BxMesh's MeshCore bot, on `#test`. Built on [meshcore-bot](https://github.com/agessaman/meshcore-bot)
v1.1.0 (MIT) and deployed to cluster-02. The design, the layout and the rules are in
[`zindello-cluster/cluster-docs`](https://github.com/zindello-cluster/cluster-docs).

| Path | What it is |
| --- | --- |
| `bathurstbot/` | The image: upstream's own meshcore-bot v1.1.0 image (pinned by digest) with our `config.ini`, the entrypoint that puts the companion's address into it, our plugins (`local/`) and their data (`data/`). Image `ghcr.io/bxmesh/bathurstbot/bathurstbot` |
| `bathurstbot/tests/` | Unit tests for the config and our plugins, run inside the image by CI (Workers AI faked) |
| `deploy/` | The chart Argo CD deploys into `dmz-bathurstbot`, from `main`, when the owner syncs (O14) |
| `.github/workflows/build.yml` | On each pull request: builds the image if it changed, tests it, pushes it tagged with the commit SHA, and writes the tag into `deploy/values.yaml` on the pull request's branch |

## Where it answers

- **Only on `#test`.** Never on Public: upstream's guard against it stays on.
- **Only to messages sent with the region scope `au`, `au-nsw` or `au-nsw-bhs`**, and it replies
  in the same scope. A message with no scope (a global flood) gets no answer. Set your scope in
  your MeshCore app.
- **Only to messages that came over at most 12 hops.**
- **DMs** are answered too, because `advert` and `schedule` work only in a DM
  (upstream's design). A DM carries no scope.

It connects to its companion radio over TCP; the address comes from OpenBao at start, never from
this repo. If the companion can't be reached when the bot starts, it exits and Kubernetes starts
it again; once connected, it reconnects by itself (every 10 s, backing off to 60 s).

**LinkWatch** (`local/service_plugins/linkwatch_service.py`, `[LinkWatch]` in `local/config.ini`)
looks after that link, and sends nothing over the air:

- **Keepalive.** Every 60 s it asks the companion for its clock. openHop's frame server closes a
  client that has sent it nothing for its `tcp_timeout`, 120 s for a companion added from openHop's
  web page without a restart, and meshcore-bot on its own sends nothing for 300 s at a time.
- **Why a message got no reply.** meshcore-bot ignores a channel message whose sender's clock is
  more than 5 s before the bot's last connection to the companion (it takes it for one the
  companion cached), and says so only at DEBUG. LinkWatch logs a warning naming the sender and how
  far behind its clock is. The fix is to set that device's clock.
- **Liveness.** It touches `/tmp/bathurstbot-alive` whenever the companion sends anything (openHop
  sends a heartbeat every 15 s); the pod's liveness probe restarts the container when the file is
  over 10 minutes old.

## Commands

| Command | Keywords | Notes |
| --- | --- | --- |
| test | `t`, `test` | Acknowledges, with the path, SNR and RSSI |
| ping | `ping` | Pong! |
| multitest | `mt`, `multitest`, `multi`, `multitrace` | Lists the different paths your message took |
| path | `path`, `decode`, `route`, `p` | Decodes your message's path into repeaters |
| trace | `trace`, `tracer` | Traces a path, e.g. `trace 01,7a,55` |
| prefix | `prefix`, `lookup`, `repeater` | Repeaters with a given prefix |
| stats | `stats` | The bot's usage over the last day |
| channels | `channels`, `channel` | BxMesh's channels: `#bathurst`, `#test` |
| advert | `advert` | **DM only.** Sends a flood advert (once an hour) |
| help | `help`, `help <command>` | The list of commands |
| cmd | `cmd`, `cmds`, `commands` | The list of commands |
| version | `version`, `ver` | meshcore-bot's version |
| contact | `contact` | The bot's contact card |
| schedule | `schedule` | **DM only.** Scheduled messages (none) and the advert interval (a flood advert every 49 hours) |
| bathurstfact | `bathurstfact`, `bxfact` | A short fact about Bathurst, Mount Panorama or the Bathurst 1000 |
| meow | `meow` | **Only on scope `au-nsw-bhs`.** A Super Troopers quote, or now and then a fact about the film |

`neighbors` is off, and so is the packet capture service it needs: the bot runs no neighbour
discovery and spends no airtime on its own. Every other built-in command is disabled by name in `bathurstbot/config.ini`, and allowed in no
channel. A few of upstream's commands are admin-only in its code whatever their switch says
(`reload`, `channelpause`, `feed`, `status`) and `gwx` has no switch at all: with no admins
configured, nobody can use them, a channel gets no reply, and a DM naming one gets "access denied".
`catfact` is off, so `meow` is ours.

## bathurstfact and meow

Each reply is short: at most half the bot's maximum message length for that reply (meshcore-bot's
own `get_max_message_length`, halved; about 68 bytes on `#test` with a scope).

**meow** answers only on the scope `au-nsw-bhs`. A `meow` sent with `au` or `au-nsw`, or in a DM,
gets no reply (meshcore-bot has no per-command scope setting, so the command checks the scope the
message came in on). About 3 times in 4 it sends **a real line from the film**, word for word from
Wikiquote (`data/quotes/meow.txt`), picked at random from those that fit; a quote is never cut, and
the AI never writes or recalls quotes. Otherwise it sends a fact, like bathurstfact.

**The facts are written by an AI from bundled text, ahead of time:**

1. A background service (`local/service_plugins/factpool_service.py`) keeps a pool of up to 10
   facts per topic on the bot's volume (`/data/factpool.json`).
2. While a pool isn't full, at most once every 3 minutes, it takes a random passage from that
   topic's bundled text (`data/bathurstfact/`, `data/meow/`: Wikipedia, and for meow also
   Wikiquote's "Cat Game" scene) and asks Cloudflare Workers AI
   (`@cf/zai-org/glm-4.7-flash`, with its reasoning turned off) for one fact from that passage
   only, within the length limit, with no preamble. Only the final answer is read.
3. A fact over the limit, or one that isn't a single plain fact, is thrown away; the next try comes
   at the next interval.
4. A reply takes the oldest pooled fact that fits, so it never waits for the AI. When the pool is
   empty, or Workers AI is down, a fact from the static list (`data/fallback/`) answers.

The Workers AI account ID and token come from OpenBao (`secret/dmz-bathurstbot/workers-ai`); the
token may only use Workers AI. A full pool makes no calls at all.

## The data and its licences

The passages, quotes and fallback facts are under **CC BY-SA 4.0**
(<https://creativecommons.org/licenses/by-sa/4.0/>), like their sources; each file's header names
its source revision and how it was changed (a selection of paragraphs, split at sentence ends;
lists, tables and passages about deaths or violence left out; quotes with strong language left
out). Fetched on 2 November 2026.

| File | Source |
| --- | --- |
| `data/bathurstfact/bathurst-new-south-wales.txt` | Wikipedia, "[Bathurst, New South Wales](https://en.wikipedia.org/w/index.php?title=Bathurst,_New_South_Wales&oldid=1382227041)", revision of 27 October 2026, by Wikipedia contributors |
| `data/bathurstfact/mount-panorama-circuit.txt` | Wikipedia, "[Mount Panorama Circuit](https://en.wikipedia.org/w/index.php?title=Mount_Panorama_Circuit&oldid=1381411992)", revision of 22 October 2026, by Wikipedia contributors |
| `data/bathurstfact/bathurst-1000.txt` | Wikipedia, "[Bathurst 1000](https://en.wikipedia.org/w/index.php?title=Bathurst_1000&oldid=1380708688)", revision of 17 October 2026, by Wikipedia contributors |
| `data/meow/super-troopers.txt` | Wikipedia, "[Super Troopers](https://en.wikipedia.org/w/index.php?title=Super_Troopers&oldid=1382157982)", revision of 27 October 2026, by Wikipedia contributors |
| `data/meow/broken-lizard.txt` | Wikipedia, "[Broken Lizard](https://en.wikipedia.org/w/index.php?title=Broken_Lizard&oldid=1378531246)", the History paragraphs about Super Troopers, by Wikipedia contributors |
| `data/meow/super-troopers-cat-game.txt` | Wikiquote, "[Super Troopers](https://en.wikiquote.org/w/index.php?title=Super_Troopers&oldid=4017742)", the "Cat Game" scene (the film's meow game) as one passage, by Wikiquote contributors |
| `data/meow/thomas-decoud.txt` | Wikipedia, "[Thomas DeCoud](https://en.wikipedia.org/w/index.php?title=Thomas_DeCoud&oldid=1378381180)", its one sentence about the film's "Meow Game", by Wikipedia contributors |
| `data/quotes/meow.txt` | Wikiquote, "[Super Troopers](https://en.wikiquote.org/w/index.php?title=Super_Troopers&oldid=4017742)", revision of 22 September 2026, by Wikiquote contributors |
| `data/fallback/*.txt` | Short facts written from the passages above |

A topic's text is changed by editing its files in `data/` by pull request; the bot reads them at
start. The AI-written facts are generated from that text, so they are as reliable as the model's
reading of one passage: short, but not checked by a person.

meshcore-bot is © its authors, MIT licence; this image uses upstream's published image unchanged,
adding our files.
