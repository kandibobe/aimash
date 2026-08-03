from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, func, select
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.prepare_prod_db import TARGET_TABLES, scrub_database


def _schema() -> MetaData:
    metadata = MetaData()
    profiles = Table("client_profiles", metadata, Column("id", Integer, primary_key=True))
    Table(
        "client_site_pages",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("profile_id", ForeignKey(profiles.c.id), nullable=False),
    )
    Table(
        "client_dossiers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("profile_id", ForeignKey(profiles.c.id), nullable=False),
    )
    proposals = Table("proposals", metadata, Column("id", Integer, primary_key=True))
    Table(
        "audit_log",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("proposal_id", ForeignKey(proposals.c.id), nullable=False),
    )
    Table("alembic_version", metadata, Column("version_num", String, primary_key=True))
    Table("app_metadata", metadata, Column("id", Integer, primary_key=True))
    return metadata


async def _counts(url: str, names: tuple[str, ...]) -> dict[str, int]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            metadata = MetaData()
            await connection.run_sync(lambda sync: metadata.reflect(bind=sync, only=list(names)))
            return {
                name: int(
                    (
                        await connection.execute(
                            select(func.count()).select_from(metadata.tables[name])
                        )
                    ).scalar_one()
                )
                for name in names
            }
    finally:
        await engine.dispose()


async def test_scrubber_is_dry_run_without_confirm_and_preserves_metadata(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'handoff.db'}"
    metadata = _schema()
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(metadata.tables["client_profiles"].insert().values(id=1))
            await connection.execute(
                metadata.tables["client_site_pages"].insert().values(id=1, profile_id=1)
            )
            await connection.execute(
                metadata.tables["client_dossiers"].insert().values(id=1, profile_id=1)
            )
            await connection.execute(metadata.tables["proposals"].insert().values(id=1))
            await connection.execute(
                metadata.tables["audit_log"].insert().values(id=1, proposal_id=1)
            )
            await connection.execute(
                metadata.tables["alembic_version"].insert().values(version_num="0041")
            )
            await connection.execute(metadata.tables["app_metadata"].insert().values(id=1))
    finally:
        await engine.dispose()

    preview = await scrub_database(url, confirm=False)
    assert preview == {name: 1 for name in TARGET_TABLES}
    assert await _counts(url, (*TARGET_TABLES, "alembic_version", "app_metadata")) == {
        **{name: 1 for name in TARGET_TABLES},
        "alembic_version": 1,
        "app_metadata": 1,
    }

    deleted = await scrub_database(url, confirm=True)
    assert deleted == preview
    assert await _counts(url, (*TARGET_TABLES, "alembic_version", "app_metadata")) == {
        **{name: 0 for name in TARGET_TABLES},
        "alembic_version": 1,
        "app_metadata": 1,
    }


async def test_scrubber_refuses_external_foreign_key_to_target(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'external-fk.db'}"
    metadata = _schema()
    Table(
        "preserved_events",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("proposal_id", ForeignKey(metadata.tables["proposals"].c.id), nullable=False),
    )
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
    finally:
        await engine.dispose()

    try:
        await scrub_database(url, confirm=True)
    except RuntimeError as error:
        assert "preserved_events -> proposals" in str(error)
    else:
        raise AssertionError("external FK must stop the scrubber")
