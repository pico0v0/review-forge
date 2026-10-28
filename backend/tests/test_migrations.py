import json

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.database_migrations import migrate


def test_existing_initial_schema_upgrades_to_current_schema(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    config = Config()
    from pathlib import Path

    config.set_main_option("script_location", str(Path(__file__).parents[1] / "app" / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO review_tasks "
                "(id, project_id, status, current_round, max_rounds, created_at, updated_at) "
                "VALUES ('REV-20260101-001', 'legacy', 'CREATED', 0, 3, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    migrate(engine)

    inspector = inspect(engine)
    assert {"review_rounds", "model_calls"}.issubset(set(inspector.get_table_names()))
    assert "snapshot_json" in {column["name"] for column in inspector.get_columns("review_tasks")}
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT source_agent, snapshot_json FROM review_tasks WHERE id='REV-20260101-001'")
        ).one()
    assert row.source_agent == "generic"
    assert json.loads(row.snapshot_json) == {}
    engine.dispose()
