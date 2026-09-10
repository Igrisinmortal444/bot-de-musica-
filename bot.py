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

    async def ok(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
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