#!/usr/bin/env bash
# Arranca el servidor POT de bgutil (Deno) y después el bot.
set -e

POT_PORT="${POT_PROVIDER_PORT:-4416}"

cd /root/bgutil-ytdlp-pot-provider/server/node_modules
nohup /root/.deno/bin/deno run --allow-env --allow-net --allow-ffi=. --allow-read=. \
    ../src/main.ts --port "$POT_PORT" >/tmp/pot_provider.log 2>&1 &
POT_PID=$!

# Espera breve para que el servidor POT esté listo
for _ in $(seq 1 10); do
    if curl -sf --max-time 2 "http://127.0.0.1:$POT_PORT/ping" >/dev/null; then
        echo "[POT] servidor POT listo en 127.0.0.1:$POT_PORT"
        break
    fi
    sleep 0.5
done

# Auto-diagnóstico: reporta el estado real del servidor POT en los logs.
if curl -sf --max-time 2 "http://127.0.0.1:$POT_PORT/ping" >/tmp/pot_ping.json; then
    echo "[POT] ping OK -> $(cat /tmp/pot_ping.json)"
else
    echo "[POT] ERROR: el servidor POT no responde en :$POT_PORT (pid $POT_PID)" >&2
    echo "[POT] --- /tmp/pot_provider.log ---" >&2
    tail -40 /tmp/pot_provider.log 2>/dev/null >&2 || echo "[POT] (sin log)" >&2
fi

exec python /app/bot.py