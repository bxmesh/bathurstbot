#!/bin/sh
# BathurstBot's entrypoint: renders the config, then runs meshcore-bot.
#
# The companion's address isn't in the image or the repo (the repo is public): it comes from
# OpenBao (secret/dmz-bathurstbot/companion, keys host and port) as files in /srv/secrets, and is
# put into /app/bathurstbot/config.ini's placeholders here, at every start. The rendered config
# goes to /run/bathurstbot (a memory-backed emptyDir), never to the volume. Nothing is printed.
set -eu
secrets=/srv/secrets
out=/run/bathurstbot/config.ini
secret() { tr -d '\r\n' < "$secrets/$1"; }

host=$(secret companion_host)
port=$(secret companion_port)
case "$host" in
  ''|*[!A-Za-z0-9.:-]*) echo "[bathurstbot] companion_host is empty or not a host name or address" >&2; exit 1 ;;
esac
case "$port" in
  ''|*[!0-9]*) echo "[bathurstbot] companion_port is empty or not a number" >&2; exit 1 ;;
esac

umask 077
awk -v host="$host" -v port="$port" \
  '{ gsub(/@@COMPANION_HOST@@/, host); gsub(/@@COMPANION_PORT@@/, port); print }' \
  /app/bathurstbot/config.ini > "$out.new"
mv "$out.new" "$out"
echo "[bathurstbot] config rendered; starting meshcore-bot ${MESHCORE_BOT_VERSION:-}"

cd /app
exec python3 meshcore_bot.py --config "$out"
