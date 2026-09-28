import pytest
from sqlalchemy.orm import Session

from signal_app.db.models import Base, SourceKind
from signal_app.db.session import make_engine
from signal_app.ingest import get_or_create_source


@pytest.fixture
def engine():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def manual_source(session):
    return get_or_create_source(session, SourceKind.MANUAL, "Test notes")


class NoSleep:
    """Records requested sleeps instead of sleeping."""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def no_sleep():
    return NoSleep()
