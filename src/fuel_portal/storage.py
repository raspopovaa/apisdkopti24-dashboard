"""Хранилище портала: таблицы, миграции, запись и чтение понятий предметной области.

Нижний слой. Пишет в него только синхронизация, читают сервисы аналитики:

    синхронизация ──► storage ──► сервисы аналитики ──► интерфейс, агент

SQLite — для пилота, PostgreSQL — для сервера; код один, адрес базы задаёт
вызывающий (``sqlite:///portal.db``, ``postgresql+psycopg://...``).
Каждая строка принадлежит договору: ``contract_key`` — непрозрачный ключ договора,
не номер и не ID из API.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Boolean,
    Column,
    Connection,
    DateTime,
    Engine,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    select,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from .domain import Card, Limit, Station, Transaction

__all__ = [
    "connect",
    "load_cards",
    "load_sync_state",
    "load_transactions",
    "metadata",
    "migrate",
    "save_cards",
    "save_limits",
    "save_stations",
    "save_sync_state",
    "save_transactions",
    "SyncState",
]


@dataclass(frozen=True, slots=True)
class SyncState:
    """Последний прогон синхронизации одного источника (операции, карты)."""

    source: str
    loaded_until: datetime | None  # данные загружены до этого момента
    finished_at: datetime
    billable_calls: int  # тарифицируемых запросов за прогон


_MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_KEY = 64  # ID и ключи из API короче
_NAME = 255


class _DecimalText(TypeDecorator[Decimal]):
    """Decimal как текст: SQLite хранит NUMERIC во float и теряет копейки."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


metadata = MetaData()

# АЗС: регион и координаты нужны географии и часовому поясу операции.
_stations = Table(
    "stations",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("id", String(_KEY), primary_key=True),
    Column("brand", String(_NAME), nullable=False),
    Column("region", String(_NAME), nullable=False),
    Column("latitude", _DecimalText(), nullable=True),
    Column("longitude", _DecimalText(), nullable=True),
)

# Карты: номер — персональные данные, наружу только через mask_card_number.
_cards = Table(
    "cards",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("id", String(_KEY), primary_key=True),
    Column("number", String(_KEY), nullable=False),
    Column("group_name", String(_NAME), nullable=True),
    Column("status", String(32), nullable=False),
    Column("product", String(_NAME), nullable=True),
    Column("home_region", String(_NAME), nullable=True),
    Column("comment", String(_NAME), nullable=True),
    Column("updated_at", DateTime(), nullable=False),
)

# Операции. Ключ — ID операции из API: повторная загрузка того же периода
# обновляет строки, а не дублирует их (сторно и корректировки приходят позже).
# Внешнего ключа на cards нет: операция может прийти по карте, которой уже нет в списке.
# local_time — местное время АЗС без пояса (поле timestamp API оканчивается на Z,
# но это не UTC); utc_time — настоящее UTC, если источник его дал.
_transactions = Table(
    "transactions",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("id", String(_KEY), primary_key=True),
    Column("local_time", DateTime(), nullable=False),
    Column("utc_time", DateTime(), nullable=True),
    Column("card_id", String(_KEY), nullable=False),
    Column("station_id", String(_KEY), nullable=False),
    Column("operation_type", String(8), nullable=True),
    Column("product", String(_NAME), nullable=False),
    Column("qty", _DecimalText(), nullable=False),
    Column("price", _DecimalText(), nullable=False),
    Column("price_no_discount", _DecimalText(), nullable=False),
    Column("amount", _DecimalText(), nullable=False),
    Column("amount_no_discount", _DecimalText(), nullable=True),
    Column("discount", _DecimalText(), nullable=False),
    Column("is_storno", Boolean(), nullable=False),
    Column("storno_of", String(_KEY), nullable=True),
    Column("raw", Text(), nullable=True),
    Column("loaded_at", DateTime(), nullable=False),
    Index("ix_transactions_period", "contract_key", "local_time"),
    Index("ix_transactions_card", "contract_key", "card_id", "local_time"),
)

# Снимок лимита на момент синхронизации: история остатка, а не только текущее значение.
_limit_snapshots = Table(
    "limit_snapshots",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("card_id", String(_KEY), primary_key=True),
    Column("product", String(_NAME), primary_key=True),
    Column("taken_at", DateTime(), primary_key=True),
    Column("liters_per_month", _DecimalText(), nullable=False),
    Column("liters_used", _DecimalText(), nullable=True),
)

# Снимок денег договора (get_contract_data — тарифицируемый, 1–2 раза в сутки).
_balance_snapshots = Table(
    "balance_snapshots",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("taken_at", DateTime(), primary_key=True),
    Column("available", _DecimalText(), nullable=False),
    Column("own_funds", _DecimalText(), nullable=True),
    Column("month_spent", _DecimalText(), nullable=True),
)

# Что и до какого момента загружено; сколько запросов пакета ушло.
_sync_state = Table(
    "sync_state",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("source", String(64), primary_key=True),
    Column("loaded_until", DateTime(), nullable=True),
    Column("finished_at", DateTime(), nullable=False),
    Column("billable_calls", Integer(), nullable=False),
)


def migrate(url: str) -> None:
    """Довести схему базы до последней миграции."""
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


def connect(url: str) -> Engine:
    """Движок базы; таблицы создаёт migrate, а не connect."""
    return create_engine(url)


def save_stations(conn: Connection, contract_key: str, stations: Iterable[Station]) -> None:
    rows = [
        {
            "contract_key": contract_key,
            "id": item.id,
            "brand": item.brand,
            "region": item.region,
            "latitude": Decimal(str(item.latitude)),
            "longitude": Decimal(str(item.longitude)),
        }
        for item in stations
    ]
    _upsert(conn, _stations, rows)


def save_cards(conn: Connection, contract_key: str, cards: Iterable[Card], now: datetime) -> None:
    rows = [
        {
            "contract_key": contract_key,
            "id": card.id,
            "number": card.number,
            "group_name": card.group,
            "status": card.status,
            "product": card.product,
            "home_region": card.home_region,
            "comment": card.comment,
            "updated_at": now,
        }
        for card in cards
    ]
    _upsert(conn, _cards, rows)


def save_transactions(
    conn: Connection, contract_key: str, items: Iterable[Transaction], now: datetime
) -> None:
    rows = [
        {
            "contract_key": contract_key,
            "id": item.id,
            "local_time": item.timestamp,
            "utc_time": item.utc_time,
            "card_id": item.card_id,
            "station_id": item.station_id,
            "operation_type": item.operation_type,
            "product": item.product,
            "qty": item.qty,
            "price": item.price,
            "price_no_discount": item.price_no_discount,
            "amount": item.amount,
            "amount_no_discount": item.amount_no_discount,
            "discount": item.discount,
            "is_storno": item.is_storno,
            "storno_of": item.storno_of,
            "raw": None,
            "loaded_at": now,
        }
        for item in items
    ]
    _upsert(conn, _transactions, rows)


def save_limits(
    conn: Connection, contract_key: str, limits: Iterable[Limit], taken_at: datetime
) -> None:
    rows = [
        {
            "contract_key": contract_key,
            "card_id": limit.card_id,
            "product": limit.product,
            "taken_at": taken_at,
            "liters_per_month": limit.liters_per_month,
            "liters_used": None,
        }
        for limit in limits
    ]
    _upsert(conn, _limit_snapshots, rows)


def load_cards(conn: Connection, contract_key: str) -> list[Card]:
    query = select(_cards).where(_cards.c.contract_key == contract_key).order_by(_cards.c.id)
    return [
        Card(
            id=row.id,
            number=row.number,
            status=row.status,
            group=row.group_name,
            comment=row.comment,
            product=row.product,
            home_region=row.home_region,
        )
        for row in conn.execute(query)
    ]


def load_transactions(
    conn: Connection, contract_key: str, date_from: date, date_to: date
) -> list[Transaction]:
    """Операции за период по местному времени АЗС, обе даты включительно."""
    start = datetime.combine(date_from, datetime.min.time())
    end = datetime.combine(date_to + timedelta(days=1), datetime.min.time())
    table = _transactions
    query = (
        select(table)
        .where(table.c.contract_key == contract_key)
        .where(table.c.local_time >= start)
        .where(table.c.local_time < end)
        .order_by(table.c.local_time, table.c.id)
    )
    return [
        Transaction(
            id=row.id,
            timestamp=row.local_time,
            card_id=row.card_id,
            station_id=row.station_id,
            product=row.product,
            qty=row.qty,
            price=row.price,
            price_no_discount=row.price_no_discount,
            amount=row.amount,
            discount=row.discount,
            is_storno=row.is_storno,
            utc_time=row.utc_time,
            operation_type=row.operation_type,
            amount_no_discount=row.amount_no_discount,
            storno_of=row.storno_of,
        )
        for row in conn.execute(query)
    ]


def save_sync_state(conn: Connection, contract_key: str, state: SyncState) -> None:
    row = {
        "contract_key": contract_key,
        "source": state.source,
        "loaded_until": state.loaded_until,
        "finished_at": state.finished_at,
        "billable_calls": state.billable_calls,
    }
    _upsert(conn, _sync_state, [row])


def load_sync_state(conn: Connection, contract_key: str, source: str) -> SyncState | None:
    query = (
        select(_sync_state)
        .where(_sync_state.c.contract_key == contract_key)
        .where(_sync_state.c.source == source)
    )
    row = conn.execute(query).first()
    if row is None:
        return None

    return SyncState(row.source, row.loaded_until, row.finished_at, row.billable_calls)


def _upsert(conn: Connection, table: Table, rows: list[dict[str, Any]]) -> None:
    """Вставить строки или обновить существующие по первичному ключу."""
    if not rows:
        return

    keys = [column.name for column in table.primary_key.columns]
    dialects: dict[str, Any] = {"sqlite": sqlite, "postgresql": postgresql}
    dialect = dialects.get(conn.dialect.name)
    if dialect is None:
        raise NotImplementedError(f"База {conn.dialect.name} не поддерживается")

    statement = dialect.insert(table)
    updates = {name: statement.excluded[name] for name in rows[0] if name not in keys}
    conn.execute(statement.on_conflict_do_update(index_elements=keys, set_=updates), rows)
