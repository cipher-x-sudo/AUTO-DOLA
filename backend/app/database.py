from collections.abc import Generator

from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {"prepare_threshold": None}
engine_kwargs = {"connect_args": connect_args, "pool_pre_ping": True}
if not is_sqlite:
    engine_kwargs["poolclass"] = NullPool

engine = create_engine(settings.database_url, **engine_kwargs)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
