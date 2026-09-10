"""MusicPowerBot — punto de entrada.

Uso:
    python bot.py

El bot corre en polling de Telegram y, en Render, levanta además un servidor
HTTP de salud en $PORT para que la plataforma no lo marque como caído.
"""

import asyncio
import logging
import os

from aiohttp import ClientSession, ClientTimeout, web
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    InlineQueryHandler,
    filters,
    MessageHandler,
)

from config import BOT_TOKEN
from handlers import callbacks, commands, inline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("charset_normalizer").setLevel(logging.WARNING)


def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", commands.start))
    app.add_handler(CommandHandler("help", commands.help_command))
    app.add_handler(CommandHandler("search", commands.search))
    app.add_handler(CommandHandler("link", commands.link))
    app.add_handler(CommandHandler("stats", commands.stats))
    app.add_handler(CallbackQueryHandler(callbacks.handle))
    app.add_handler(InlineQueryHandler(inline.inline_query))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, commands.on_message))

    return app


async def health_server() -> None:
    """Servidor HTTP de salud para Render ($PORT); el bot sigue en polling."""

    from services import downloader

    async def ok(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def pot_status(_: web.Request) -> web.Response:
        """Estado del servidor POT (para depurar desde fuera)."""
        import json
        import urllib.request

        port = int(os.getenv("POT_PROVIDER_PORT", "4416"))
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/ping", timeout=3
            ) as resp:
                data = json.load(resp)
            return web.json_response({"pot": "ok", **data})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"pot": "error", "error": str(exc)})

    async def yt_check(request: web.Request) -> web.Response:
        """Autoprueba: descarga una canción de YouTube dentro del container.

        Permite reproducir dentro de Render el MISMO camino que sigue el bot
        (ytsearch1 → cascada de clientes → POT) y ver el error exacto aquí.
        """
        import asyncio
        import time

        q = (request.query.get("q") or "Never Gonna Give You Up").strip()
        fmt = (request.query.get("fmt") or "m4a").strip().lower()
        if fmt not in ("m4a", "mp3"):
            fmt = "m4a"
        t0 = time.time()
        try:
            res = await asyncio.wait_for(
                downloader.download(f"ytsearch1:{q}", fmt, None), timeout=240
            )
            return web.json_response(
                {
                    "ok": True,
                    "elapsed_s": round(time.time() - t0, 1),
                    "title": res.get("title"),
                    "artist": res.get("artist"),
                    "duration_s": res.get("duration"),
                    "ext": res.get("ext"),
                    "bytes": os.path.getsize(res["file_path"]),
                    "source": res.get("source"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "elapsed_s": round(time.time() - t0, 1), "error": str(exc)}
            )

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    app.router.add_get("/pot", pot_status)
    app.router.add_get("/ytcheck", yt_check)
    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info("🩺 Health HTTP server en puerto %s", port)


async def keepalive() -> None:
    """Ping silencioso cada 13 min para evitar el sleep de Render (free: 15 min)."""
    external = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not external:
        return
    url = f"{external}/health"
    timeout = ClientTimeout(total=15)
    async with ClientSession() as session:
        while True:
            await asyncio.sleep(13 * 60)
            try:
                async with session.get(url, timeout=timeout) as resp:
                    await resp.read()
            except Exception:
                pass


async def main() -> None:
    app = build_app()
    logging.info("✅ Bot arrancando con token %s…", BOT_TOKEN[:8] + "…")
    await app.initialize()
    await health_server()
    asyncio.create_task(keepalive())
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await app.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())