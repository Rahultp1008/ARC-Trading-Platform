# alembic/env.py
# ---------------------------------------------------------------------------
# Alembic uses this file to connect to the database and discover your models.
#
# Key things we configure here:
#   1. Point Alembic at our DATABASE_URL from settings (not from alembic.ini).
#   2. Pass `target_metadata` so `alembic revision --autogenerate` can diff
#      your models against the current DB schema.
# ---------------------------------------------------------------------------
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Load our app settings
from app.core.config import settings

# Import Base AND all models so Alembic's autogenerate can see every table.
from app.core.database import Base
import app.models  # noqa: F401 — side-effect import registers all models on Base
import app.models.user
import app.models.refresh_token
import app.models.login_audit
import app.models.instruments  #  ADDED

# Alembic config object (wraps alembic.ini)
config = context.config

# Inject the real DB URL from our settings so we don't hard-code it
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata Alembic compares against when autogenerating migrations
target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline mode — generates SQL without connecting to the DB
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url            = url,
        target_metadata= target_metadata,
        literal_binds  = True,
        dialect_opts   = {"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode — connects to DB and applies migrations directly
# ---------------------------------------------------------------------------
def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix         = "sqlalchemy.",
        poolclass      = pool.NullPool,  # no pooling needed for migration runs
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
    
 