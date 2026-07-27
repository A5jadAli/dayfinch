from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager
from datetime import datetime, timezone

from psycopg import Connection


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RepositoryMixin:
    def connect(self) -> AbstractContextManager[Connection]:
        raise NotImplementedError
