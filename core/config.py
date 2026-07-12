import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


class Settings(BaseModel):
    openai_api_key: str = ""
    google_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"

    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "afos-dev"

    afos_db_path: str = "database/afos.db"

    afos_monthly_budget_usd: float = 50.0
    afos_venture_budget_usd: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings(
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        google_api_key=os.environ.get("GOOGLE_API_KEY", ""),
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        langchain_tracing_v2=os.environ.get("LANGCHAIN_TRACING_V2", "false").lower() == "true",
        langchain_api_key=os.environ.get("LANGCHAIN_API_KEY", ""),
        langchain_project=os.environ.get("LANGCHAIN_PROJECT", "afos-dev"),
        afos_db_path=os.environ.get("AFOS_DB_PATH", "database/afos.db"),
        afos_monthly_budget_usd=float(os.environ.get("AFOS_MONTHLY_BUDGET_USD", "50.0")),
        afos_venture_budget_usd=float(os.environ.get("AFOS_VENTURE_BUDGET_USD", "10.0")),
    )
