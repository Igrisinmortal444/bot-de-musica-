#!/usr/bin/env bash
# Calienta la caché de compilación de Deno para el servidor POT durante el
# build. El primer `deno run` de main.ts transpila todo el grafo (~2 min en
# máquinas lentas); al hacerlo aquí, en runtime arranca en segundos.
set +e
cd /root/bgutil-ytdlp-pot-provider/server/node_modules 2>/dev/null || exit 0
(/root/.deno/bin/deno run \
    --allow-env --allow-net --allow-ffi=. --allow-read=. \
    ../src/main.ts --port 4417 </dev/null >/tmp/pot_warmup.log 2>&1 &)
sleep 180
pkill -f "src/main.ts" 2>/dev/null
pkill -f "src/main.ts --port 4417" 2>/dev/null
exit 0