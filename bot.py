"""MusicPowerBot — punto de entrada.

Uso:
    python bot.py
"""

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    filters,
    MessageHandler,
)

from config import BOT_TOKEN
from handlers import callbacks, commands

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
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, commands.on_message))

    return app


def main() -> None:
    app = build_app()
    logging.info("✅ Bot arrancando con token %s…", BOT_TOKEN[:8] + "…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()