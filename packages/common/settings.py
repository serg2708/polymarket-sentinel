from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://postgres:changeme@localhost:5432/polysentinel"
    database_url_sync: str = "postgresql://postgres:changeme@localhost:5432/polysentinel"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Telegram
    telegram_bot_token: str = ""
    admin_chat_id: int = 0

    # Kalshi
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_private_key_b64: str = ""

    # Sportsbook
    oddspapi_api_key: str = ""

    # News
    newsapi_key: str = ""

    # NVIDIA NIM (primary LLM when key is set)
    nvidia_api_key: str = ""
    nvidia_model: str = "meta/llama-3.3-70b-instruct"        # reasoning: tail_risk, llm_prior
    nvidia_fast_model: str = "meta/llama-3.1-8b-instruct"    # high-volume: news R/L/U scoring
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"

    # Ollama (fallback when NIM key absent, or primary when ollama_primary=true)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:14b-instruct"
    ollama_primary: bool = False

    # Optional LLM APIs
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Quiet hours: suppress Telegram alerts (CET timezone, 24h format)
    quiet_hours_start: int = 22   # 22:00 CET
    quiet_hours_end: int = 8      # 08:00 CET

    # Polymarket
    polymarket_address: str = ""        # Ethereum wallet address (0x...)

    # Metaculus
    metaculus_api_token: str = ""

    # Alert thresholds
    arb_min_edge_bps: int = 100
    soft_edge_min_bps: int = 500
    soft_edge_min_pp: float = 5.0       # minimum absolute edge in percentage points
    liquidity_spread_threshold: float = 0.05
    liquidity_min_mid: float = 0.05  # ignore tokens priced below this (noise filter)

    # Ingest
    gamma_poll_interval_seconds: int = 300
    price_poll_interval_seconds: int = 10
    top_markets_by_volume: int = 500
    max_markets_per_event: int = 8
    # Comma-separated Polymarket market IDs to always track regardless of volume rank
    supplemental_market_ids: str = ""
    # Comma-separated market IDs to never alert on
    blocked_market_ids: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
