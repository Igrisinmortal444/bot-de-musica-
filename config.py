import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError(
        "Falta BOT_TOKEN. Crea un archivo .env con tu token de BotFather (mira .env.example)."
    )

OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
WORKERS = max(1, int(os.getenv("WORKERS", "4")))
SEARCH_RESULTS = int(os.getenv("SEARCH_RESULTS", "12"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "45"))

ITUNES_COUNTRIES = [
    c.strip()
    for c in os.getenv(
        "ITUNES_COUNTRIES",
        "US,GB,DE,FR,ES,MX,BR,AR,CO,JP,KR,IN,NG,TR,IT",
    ).split(",")
    if c.strip()
]