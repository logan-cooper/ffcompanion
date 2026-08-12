"""Typed settings loaded from the environment and `.env`.

No secrets in code. Anything configurable is a field here, and every other
module reads it through `get_settings()`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/advisor/config.py -> src/advisor -> src -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application settings.

    Values come from environment variables first, then `.env`, then the
    defaults below. Relative paths are resolved against the project root so
    commands behave the same regardless of the working directory.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Inference runs locally through Ollama. There is no API key and no
    # per-token cost anywhere in this app — see docs/ROADMAP.md Phase 5.
    model: str = Field(
        default="qwen3:8b",
        description="Ollama model tag used for the agent loop.",
    )
    ollama_host: str = Field(
        default="http://localhost:11434",
        description="Base URL of the local Ollama server.",
    )
    db_path: Path = Field(
        default=Path("data/advisor.duckdb"),
        description="Path to the local DuckDB file.",
    )
    cache_dir: Path = Field(
        default=Path("data/cache"),
        description="Directory for cached raw Parquet and API payloads.",
    )
    default_league_id: str | None = Field(
        default=None,
        description="Sleeper league id used when a command doesn't name one.",
    )

    @field_validator("db_path", "cache_dir")
    @classmethod
    def _resolve_against_project_root(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object."""
    return Settings()
