# app/core/database.py
# ---------------------------------------------------------------------------
# Sets up the SQLAlchemy engine and session factory.
#
# Key concepts for a fresher:
#   - engine      → the connection to PostgreSQL
#   - SessionLocal → a "factory" that produces individual DB sessions
#   - Base        → every SQLAlchemy model inherits from this; it lets
#                   Alembic discover all tables automatically
# ---------------------------------------------------------------------------

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.core.config import settings


# The engine manages the underlying connection pool to PostgreSQL.
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,   # re-checks a connection before using it (avoids stale connections)
    echo=False,           # set True locally if you want to see raw SQL in the console
)

# Each call to SessionLocal() returns a new DB session scoped to one request.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Declarative base class — all models inherit from this.
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# FastAPI dependency — yields a DB session and guarantees it is closed after
# the request finishes (even if an exception is raised).
# Usage: `db: Session = Depends(get_db)` inside any endpoint.
# ---------------------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
