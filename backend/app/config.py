"""Local MVP configuration; secrets never belong in source control."""
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')

    database_url: str | None = None
    query_timeout_ms: int = Field(default=5000, ge=100, le=30000)
    llm_provider: Literal['demo', 'openai_compatible', 'deepseek'] = 'demo'
    llm_base_url: str = 'https://api.openai.com/v1'
    llm_api_key: SecretStr | None = None
    llm_model: str = ''
    llm_max_output_tokens: int = Field(default=700, ge=256, le=2048)
    llm_cache_seconds: int = Field(default=3600, ge=0, le=86400)
    llm_timeout_seconds: int = Field(default=30, ge=1, le=120)
    agent_max_steps: int = Field(default=12, ge=1, le=30)
    agent_max_duration_ms: int = Field(default=60000, ge=100, le=180000)
    agent_max_model_calls: int = Field(default=1, ge=0, le=1)
    agent_max_tokens: int = Field(default=4096, ge=256, le=16384)
    analysis_store: Path = ROOT / 'data' / 'artifacts' / 'analyses.sqlite3'


settings = Settings()
