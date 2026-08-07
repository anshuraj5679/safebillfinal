from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import get_settings
settings = get_settings()


def _build_engine_url(raw_url: str) -> str:
    if not raw_url or "[YOUR-DB-PASSWORD]" in raw_url:
        return "sqlite:///./safebill.db"
    try:
        url = make_url(raw_url)
    except Exception:
        return raw_url

    driver = url.drivername.split("+", 1)[0].lower()
    if driver not in {"postgresql", "postgres"}:
        return raw_url

    if "+" not in url.drivername:
        url = url.set(drivername="postgresql+psycopg")

    query = dict(url.query)
    if "connect_timeout" not in query:
        query["connect_timeout"] = "5"
    return url.set(query=query).render_as_string(hide_password=False)


db_url = _build_engine_url(settings.database_url)
is_sqlite = db_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}

from sqlalchemy import event
import sqlite3

try:
    engine = create_engine(db_url, pool_pre_ping=True, future=True, connect_args=connect_args)
except Exception:
    engine = create_engine("sqlite:///./safebill.db", pool_pre_ping=True, future=True, connect_args={"check_same_thread": False})

@event.listens_for(engine, "connect")
def connect(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.create_function("to_tsvector", 2, lambda lang, text: text, deterministic=True)
        dbapi_connection.create_function("array_to_string", 2, lambda arr, sep: str(arr), deterministic=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
