"""Settings loaded from .env (see .env.example)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Hosted, OpenAI-compatible providers that serve Qwen for free.
PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "default_model": "qwen/qwen3.8-27b",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "default_model": "qwen/qwen3-235b-a22b:free",
    },
}

# Knowledge-base folder under knowledge/ holding the resume and project write-ups.
COLLECTION = "resume"


@dataclass
class Settings:
    provider: str
    base_url: str
    api_key: str
    model: str
    # Optional embeddings endpoint; when unset, retrieval uses BM25.
    embed_base_url: str
    embed_api_key: str
    embed_model: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    sender_name: str
    imap_host: str
    apify_token: str
    followup_days: list[int]  # days after the previous email when each follow-up is due

    @property
    def use_embeddings(self) -> bool:
        return bool(self.embed_base_url and self.embed_model)


def load_settings() -> Settings:
    provider = os.getenv("LLM_PROVIDER", "groq").lower()
    if provider not in PROVIDERS:
        raise ValueError(f"LLM_PROVIDER must be one of {list(PROVIDERS)}, got {provider!r}")
    p = PROVIDERS[provider]
    return Settings(
        provider=provider,
        base_url=p["base_url"],
        api_key=os.getenv(p["key_env"], ""),
        model=os.getenv("LLM_MODEL") or p["default_model"],
        embed_base_url=os.getenv("EMBED_BASE_URL", ""),
        embed_api_key=os.getenv("EMBED_API_KEY", ""),
        embed_model=os.getenv("EMBED_MODEL", ""),
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        sender_name=os.getenv("SENDER_NAME", ""),
        imap_host=os.getenv("IMAP_HOST", "imap.gmail.com"),
        apify_token=os.getenv("APIFY_API_TOKEN", ""),
        followup_days=[int(d) for d in os.getenv("FOLLOWUP_DAYS", "4,7").split(",") if d.strip()],
    )
