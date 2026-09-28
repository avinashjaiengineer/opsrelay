from functools import lru_cache

from ..config import get_settings
from .base import Record, Store, new_approval_id, new_incident_id, now_iso

__all__ = ["Record", "Store", "get_store", "new_approval_id", "new_incident_id", "now_iso"]


@lru_cache
def get_store() -> Store:
    settings = get_settings()
    if settings.store == "dynamodb":
        from .dynamodb import DynamoStore

        return DynamoStore(settings.dynamodb_table, region=settings.aws_region, endpoint=settings.dynamodb_endpoint)
    from .sqlite import SqliteStore

    return SqliteStore(settings.sqlite_path)
