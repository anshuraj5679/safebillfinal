"""SafeBill RAG backend package."""

from sqlalchemy.types import TypeDecorator, Text
import json

class SqliteCompatibleArray(TypeDecorator):
    impl = Text
    cache_ok = True

    def __init__(self, item_type=None, *args, **kwargs):
        super().__init__()
        self.item_type = item_type

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
            return dialect.type_descriptor(PG_ARRAY(self.item_type or Text))
        else:
            return dialect.type_descriptor(Text)

    def process_bind_param(self, value, dialect):
        if dialect.name == "postgresql":
            return value
        if value is None:
            return None
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if dialect.name == "postgresql":
            return value
        if value is None:
            return []
        try:
            return json.loads(value)
        except Exception:
            return []

import sqlalchemy.dialects.postgresql
sqlalchemy.dialects.postgresql.ARRAY = SqliteCompatibleArray

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID

@compiles(TSVECTOR, "sqlite")
def compile_tsvector_sqlite(element, compiler, **kw):
    return "TEXT"

@compiles(UUID, "sqlite")
def compile_uuid_sqlite(element, compiler, **kw):
    return "TEXT"

