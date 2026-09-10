#!/usr/bin/env bash
# Arranca el servidor POT de bgutil (Deno) y luego el bot.
# Espera hasta que el POT responde /ping (con caché tibia son pocos segundos);
# si tardara más, el bot igualmente arranca (las descargas de YouTube pueden
# retrasarse un poco pero no mueren).
set -e

POT_PORT="${POT_PROVIDER_PORT:-4416}"
MAX_POT_WAIT=300

cd /root/bgutil-ytdlp-pot-provider/server/node_modules
nohup /root/.deno/bin/deno run --allow-env --allow-net --allow-ffi=. --allow-read=. \
    ../src/main.ts --port "$POT_PORT" >/tmp/pot_provider.log 2>&1 &
POT_PID=$!

# Espera ACTIVA: sigue intentando el ping hasta que el POT esté listo.
wait_seconds=0
ping_ok=0
while [ "$wait_seconds" -lt "$MAX_POT_WAIT" ]; do
    if curl -sf --max-time 2 "http://127.0.0.1:$POT_PORT/ping" >/tmp/pot_ping.json 2>/dev/null; then
        ping_ok=1
        break
    fi
    sleep 1
    wait_seconds=$((wait_seconds + 1))
done

if [ "$ping_ok" = "1" ]; then
    echo "[POT] listo en 127.0.0.1:$POT_PORT tras ${wait_seconds}s -> $(cat /tmp/pot_ping.json)"
else
    echo "[POT] ERROR: no responde en :$POT_PORT tras ${wait_seconds}s (pid $POT_PID)" >&2
    echo "[POT] --- /tmp/pot_provider.log ---" >&2
    tail -40 /tmp/pot_provider.log 2>/dev/null >&2 || echo "[POT] (sin log)" >&2
fi

exec python /app/bot.py