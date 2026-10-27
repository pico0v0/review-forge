from alembic import context

from app import models  # noqa: F401
from app.db import Base

context.configure(connection=context.config.attributes["connection"], target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()
