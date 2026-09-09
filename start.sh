#!/usr/bin/env bash
# Arranca el servidor POT de bgutil (Deno) y después el bot.
set -e

POT_PORT="${POT_PROVIDER_PORT:-4416}"

cd /root/bgutil-ytdlp-pot-provider/server/node_modules
nohup /root/.deno/bin/deno run --allow-env --allow-net --allow-ffi=. --allow-read=. \
    ../src/main.ts --port "$POT_PORT" >/tmp/pot_provider.log 2>&1 &

# Espera breve para que el servidor POT esté listo
for _ in $(seq 1 10); do
    if curl -sf --max-time 1 "http://127.0.0.1:$POT_PORT" >/dev/null; then
        break
    fi
    sleep 0.5
done

exec python /app/bot.py