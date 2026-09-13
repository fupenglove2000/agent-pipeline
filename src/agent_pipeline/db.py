"""Minimal engine/session setup. database_url comes from Settings."""

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from agent_pipeline.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url)


def get_session() -> Session:
    # expire_on_commit=False: callers (e.g. runner/single.py) return ORM
    # objects out of the session's `with` block after a commit; the default
    # would expire their attributes on commit and then fail to reload them
    # once the session is closed.
    return Session(get_engine(), expire_on_commit=False)
