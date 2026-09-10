#!/usr/bin/env bash
# Pre-compila el grafo del servidor POT (Deno) DURANTE el build para que en
# runtime arranque en segundos. Espera hasta que /ping responde (garantiza que
# toda la compilación terminó) y solo entonces mata el proceso. Si no logra
# arrancar en el tiempo máximo, el build continúa igualmente (|| true) y el
# servidor PODRÁ arrancar en runtime, aunque tarde más.
set +e

PORT=${POT_WARMUP_PORT:-4417}
LOG=/tmp/pot_warmup.log
MAX_WAIT=${POT_WARMUP_MAX_WAIT:-420}

cd /root/bgutil-ytdlp-pot-provider/server/node_modules 2>/dev/null || exit 0

nohup /root/.deno/bin/deno run \
    --allow-env --allow-net --allow-ffi=. --allow-read=. \
    ../src/main.ts --port "$PORT" >"$LOG" 2>&1 &
POT_PID=$!

echo "[warmup] POT pid $POT_PID, esperando /ping en 127.0.0.1:$PORT (max ${MAX_WAIT}s)…"

ok=0
for _ in $(seq 1 "$MAX_WAIT"); do
    if curl -sf --max-time 2 "http://127.0.0.1:$PORT/ping" >/tmp/pot_warmup_ping.json 2>/dev/null; then
        ok=1
        echo "[warmup] POT listo: $(cat /tmp/pot_warmup_ping.json)"
        break
    fi
    sleep 1
done

if [ "$ok" = "1" ]; then
    # Caché de compilación ya está escrita: se detiene limpiamente.
    pkill -f "src/main.ts --port $PORT" 2>/dev/null
    pkill -f "src/main.ts" 2>/dev/null
    exit 0
fi

echo "[warmup] ADVERTENCIA: POT no respondió en ${MAX_WAIT}s; el runtime tendrá que compilar (log:)"
tail -20 "$LOG" 2>/dev/null
pkill -f "src/main.ts" 2>/dev/null
exit 0