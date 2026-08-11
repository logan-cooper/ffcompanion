"""Local stats warehouse: schema, ingestion, and derived views. Phase 1."""

from advisor.warehouse.ingest import ingest_season
from advisor.warehouse.schema import create_schema

__all__ = ["create_schema", "ingest_season"]
