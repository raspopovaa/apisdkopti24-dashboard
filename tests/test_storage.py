from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, func, select, text

from fuel_portal.storage import (
    connect,
    load_cards,
    load_transactions,
    metadata,
    migrate,
    save_cards,
    save_limits,
    save_stations,
    save_transactions,
)
from fuel_portal.synthetic import SyntheticDataset, generate

CONTRACT = "syn-contract-1"
OTHER_CONTRACT = "syn-contract-2"
NOW = datetime(2026, 10, 24, 12, 0)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    url = f"sqlite:///{tmp_path / 'portal.db'}"
    migrate(url)
    return connect(url)


@pytest.fixture
def dataset() -> SyntheticDataset:
    return generate()


def _save(engine: Engine, contract: str, dataset: SyntheticDataset) -> None:
    with engine.begin() as conn:
        save_stations(conn, contract, dataset.stations)
        save_cards(conn, contract, dataset.cards, NOW)
        save_transactions(conn, contract, dataset.transactions, NOW)
        save_limits(conn, contract, dataset.limits, NOW)


def _period(dataset: SyntheticDataset) -> tuple[date, date]:
    days = [item.timestamp.date() for item in dataset.transactions]
    return min(days), max(days)


def test_migration_matches_tables(engine: Engine) -> None:
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)

    assert diff == []


def test_round_trip_keeps_exact_values(engine: Engine, dataset: SyntheticDataset) -> None:
    _save(engine, CONTRACT, dataset)

    with engine.connect() as conn:
        cards = load_cards(conn, CONTRACT)
        transactions = load_transactions(conn, CONTRACT, *_period(dataset))

    assert cards == sorted(dataset.cards, key=lambda card: card.id)
    assert transactions == list(dataset.transactions)


def test_period_bounds_are_inclusive(engine: Engine, dataset: SyntheticDataset) -> None:
    _save(engine, CONTRACT, dataset)
    day = dataset.transactions[len(dataset.transactions) // 2].timestamp.date()

    with engine.connect() as conn:
        loaded = load_transactions(conn, CONTRACT, day, day)

    assert loaded == [item for item in dataset.transactions if item.timestamp.date() == day]


def test_reload_updates_instead_of_duplicating(engine: Engine, dataset: SyntheticDataset) -> None:
    _save(engine, CONTRACT, dataset)
    first = dataset.transactions[0]
    corrected = replace(first, amount=first.amount + Decimal("0.01"))

    with engine.begin() as conn:
        save_transactions(conn, CONTRACT, dataset.transactions, NOW)
        save_transactions(conn, CONTRACT, [corrected], NOW)

    with engine.connect() as conn:
        count = conn.execute(select(func.count()).select_from(text("transactions"))).scalar()
        loaded = load_transactions(conn, CONTRACT, *_period(dataset))

    assert count == len(dataset.transactions)
    assert loaded[0] == corrected


def test_contracts_do_not_mix(engine: Engine, dataset: SyntheticDataset) -> None:
    _save(engine, CONTRACT, dataset)

    with engine.connect() as conn:
        assert load_cards(conn, OTHER_CONTRACT) == []
        assert load_transactions(conn, OTHER_CONTRACT, *_period(dataset)) == []
