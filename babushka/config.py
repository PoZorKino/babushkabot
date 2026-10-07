import os

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"{name} не задан (см. .env.example)")
    return value


BOT_TOKEN = _required("TELEGRAM_BOT_TOKEN")
API_KEY = _required("LLM_API_KEY")
BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
MODELS = [m.strip() for m in os.getenv("LLM_MODELS", "openrouter/free").split(",") if m.strip()]
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "12"))
