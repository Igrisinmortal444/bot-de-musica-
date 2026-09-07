#!/usr/bin/env bash
# Inicia MusicPowerBot en segundo plano (detachado) con logs en bot.log
cd "$(dirname "$0")"
BOT_PATH="$PWD/bot.py"
# "$PWD/[b]ot.py" (truco de corchetes) evita que pgrep se autocoincida con este shell
if pgrep -f "$PWD/[b]ot.py" >/dev/null; then
    echo "El bot ya está corriendo."
    exit 0
fi
setsid .venv/bin/python "$BOT_PATH" </dev/null >>bot.log 2>&1 &
sleep 3
if pgrep -f "$PWD/[b]ot.py" >/dev/null; then
    echo "✅ Bot arrancado. Logs: bot.log"
else
    echo "❌ No arrancó. Revisa bot.log"
fi