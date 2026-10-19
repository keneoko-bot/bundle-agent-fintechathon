"""Settings, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODELS = {
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-sonnet-4-5",
}


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so we don't need an extra dependency."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]  # allow KEY="value" or KEY='value'
        if key and value and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    database_url: str
    llm_provider: str  # "openai" | "anthropic" | "none"
    llm_model: str | None
    openai_api_key: str | None
    anthropic_api_key: str | None
    campus_domains: tuple[str, ...]
    llm_base_url: str | None = None  # any OpenAI-compatible endpoint (DeepSeek, Qwen, Ollama...)
    llm_reasoning_effort: str | None = None  # "auto" picks a value the model needs for tool calls


def load_settings() -> Settings:
    _load_dotenv(Path.cwd() / ".env")
    openai_key = os.getenv("OPENAI_API_KEY") or None
    anthropic_key = os.getenv("ANTHROPIC_API_KEY") or None
    provider = (os.getenv("BUNDLE_LLM_PROVIDER") or "auto").lower()
    if provider == "auto":
        provider = "openai" if openai_key else "anthropic" if anthropic_key else "none"
    if provider == "openai" and not openai_key:
        provider = "none"
    if provider == "anthropic" and not anthropic_key:
        provider = "none"
    model = os.getenv("BUNDLE_LLM_MODEL") or DEFAULT_MODELS.get(provider)
    domains = tuple(
        d.strip().lower()
        for d in (os.getenv("BUNDLE_CAMPUS_DOMAINS") or "cornell.edu").split(",")
        if d.strip()
    )
    return Settings(
        database_url=os.getenv("BUNDLE_DATABASE_URL") or "sqlite:///./bundle.db",
        llm_provider=provider,
        llm_model=model if provider != "none" else None,
        openai_api_key=openai_key,
        anthropic_api_key=anthropic_key,
        campus_domains=domains,
        llm_base_url=os.getenv("BUNDLE_LLM_BASE_URL") or None,
        llm_reasoning_effort=(os.getenv("BUNDLE_LLM_REASONING_EFFORT") or "auto").lower(),
    )
