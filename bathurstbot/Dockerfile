# BathurstBot: meshcore-bot (github.com/agessaman/meshcore-bot, MIT) with BxMesh's config, our own
# plugins (local/) and their data (data/).
#
# meshcore-bot v1.1.0, upstream's own published image, pinned by digest (ghcr.io, read 2 November
# 2026; its org.opencontainers.image.revision label is 7a6580c, the v1.1.0 tag's commit). An update
# is a pull request with its changelog, risks and rollback: change both lines together.
FROM ghcr.io/agessaman/meshcore-bot:1.1.0@sha256:5e2425198b70d71e89f5f29710b85b95b2433e98512a9673dfd226380044fa91

# Ours, owned by root and read-only to the bot: the config template (the entrypoint renders it at
# each start), the plugins, merged into upstream's /app/local, and the data they read.
USER root
# Upstream's config.ini.example, which its published image leaves out: meshcore-bot checks config
# keys against it, and without it warns about every valid [Bot] key. From the v1.1.0 tag's commit,
# checked against its SHA-256.
RUN python3 - <<'PY'
import hashlib, urllib.request
url = ("https://raw.githubusercontent.com/agessaman/meshcore-bot/"
       "7a6580c608b6b9e5ac2f7df8c236c875e5cb4b22/config.ini.example")
want = "0928cf75bc2a1fa4e70fa8a0ce3e59ab8058deada97880ea2d45608fce6d9fab"
data = urllib.request.urlopen(url, timeout=60).read()
assert hashlib.sha256(data).hexdigest() == want, "config.ini.example: checksum mismatch"
open("/app/config.ini.example", "wb").write(data)
PY
COPY config.ini /app/bathurstbot/config.ini
COPY data/ /app/bathurstbot/data/
COPY local/ /app/local/
COPY --chmod=0755 bathurstbot-entrypoint.sh /bathurstbot-entrypoint.sh
RUN chmod -R a+rX /app/bathurstbot /app/local /app/config.ini.example

# Upstream's unprivileged user, meshcore (uid 1000): the dmz zone's Pod Security is restricted.
# The volume (/data) is the pod's group's (fsGroup 1000, in the chart).
USER 1000:1000
ENTRYPOINT ["/bathurstbot-entrypoint.sh"]
CMD []
