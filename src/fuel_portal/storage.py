"""Хранилище портала: таблицы, миграции, запись и чтение понятий предметной области.

Нижний слой. Пишет в него только синхронизация, читают сервисы аналитики:

    синхронизация ──► storage ──► сервисы аналитики ──► интерфейс, агент

SQLite — для пилота, PostgreSQL — для сервера; код один, адрес базы задаёт
вызывающий (``sqlite:///portal.db``, ``postgresql+psycopg://...``).
Каждая строка принадлежит договору: ``contract_key`` — непрозрачный ключ договора,
не номер и не ID из API.
"""

from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Boolean,
    Column,
    Connection,
    Date,
    DateTime,
    Engine,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from .domain import (
    Card,
    CardGroup,
    ContractSnapshot,
    Document,
    Limit,
    Payment,
    Station,
    StatusChange,
    Transaction,
)

__all__ = [
    "connect",
    "count_cards",
    "load_card_groups",
    "load_cards",
    "load_contract_snapshot",
    "load_documents",
    "load_limits",
    "load_payments",
    "load_status_history",
    "load_sync_state",
    "load_transactions",
    "metadata",
    "migrate",
    "save_card_groups",
    "save_cards",
    "save_contract_snapshot",
    "save_documents",
    "save_limits",
    "save_payments",
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
# Цель — карта или группа; коды периода — как в API.
_limit_snapshots = Table(
    "limit_snapshots",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("limit_id", String(_KEY), primary_key=True),
    Column("taken_at", DateTime(), primary_key=True),
    Column("card_id", String(_KEY), nullable=True),
    Column("group_id", String(_KEY), nullable=True),
    Column("product", String(_NAME), nullable=False),
    Column("liters", _DecimalText(), nullable=True),
    Column("liters_used", _DecimalText(), nullable=True),
    Column("unit", String(16), nullable=True),
    Column("amount", _DecimalText(), nullable=True),
    Column("amount_used", _DecimalText(), nullable=True),
    Column("period_type", Integer(), nullable=True),
    Column("period_number", Integer(), nullable=True),
)

# Группы карт: число карт в группе — для сверки со списком карт.
_card_groups = Table(
    "card_groups",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("id", String(_KEY), primary_key=True),
    Column("name", String(_NAME), nullable=False),
    Column("cards_count", Integer(), nullable=False),
    Column("updated_at", DateTime(), nullable=False),
)

# История статусов карт: строка на каждую замеченную смену статуса.
# Объясняет отказы и операции по заблокированной карте задним числом.
_card_status_history = Table(
    "card_status_history",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("card_id", String(_KEY), primary_key=True),
    Column("seen_at", DateTime(), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("previous_status", String(32), nullable=True),
)

# Снимок денег и условий договора (get_contract_data — тарифицируемый, 1 запрос).
_contract_snapshots = Table(
    "contract_snapshots",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("taken_at", DateTime(), primary_key=True),
    Column("available", _DecimalText(), nullable=False),
    Column("own_funds", _DecimalText(), nullable=False),
    Column("balance", _DecimalText(), nullable=False),
    Column("month_spent", _DecimalText(), nullable=False),
    Column("month_liters", _DecimalText(), nullable=False),
    Column("prev_month_liters", _DecimalText(), nullable=False),
    Column("status", String(32), nullable=False),
    Column("pay_scheme", String(32), nullable=False),
    Column("cards_total", Integer(), nullable=False),
    Column("cards_active", Integer(), nullable=False),
    Column("credit_limit", _DecimalText(), nullable=True),
    Column("amount_limiter_used", _DecimalText(), nullable=True),
    Column("amount_limiter_left", _DecimalText(), nullable=True),
    Column("last_payment_amount", _DecimalText(), nullable=True),
    Column("last_payment_date", Date(), nullable=True),
)

# Поступления на договор (get_payments — тарифицируемый, 1 запрос).
_payments = Table(
    "payments",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("id", String(_KEY), primary_key=True),
    Column("paid_at", DateTime(), nullable=False),
    Column("amount", _DecimalText(), nullable=False),
    Column("amount_client", _DecimalText(), nullable=False),
    Column("payment_type", String(_NAME), nullable=False),
    Column("payment_name", String(_NAME), nullable=False),
    Column("number", String(_KEY), nullable=True),
    Column("description", Text(), nullable=True),
    Column("loaded_at", DateTime(), nullable=False),
    Index("ix_payments_period", "contract_key", "paid_at"),
)

# Закрывающие документы (get_documents — бесплатный): итог месяца от оператора.
_documents = Table(
    "documents",
    metadata,
    Column("contract_key", String(_KEY), primary_key=True),
    Column("id", String(_KEY), primary_key=True),
    Column("name", String(_NAME), nullable=False),
    Column("number", String(_KEY), nullable=False),
    Column("issued_at", DateTime(), nullable=False),
    Column("total", _DecimalText(), nullable=False),
    Column("vat", _DecimalText(), nullable=False),
    Column("amount", _DecimalText(), nullable=False),
    Column("loaded_at", DateTime(), nullable=False),
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
    """Обновить карты и записать в историю каждую смену статуса."""
    cards = list(cards)
    query = select(_cards.c.id, _cards.c.status).where(_cards.c.contract_key == contract_key)
    known: dict[str, str] = {row.id: row.status for row in conn.execute(query)}
    changes = [
        {
            "contract_key": contract_key,
            "card_id": card.id,
            "seen_at": now,
            "status": card.status,
            "previous_status": known.get(card.id),
        }
        for card in cards
        if known.get(card.id) != card.status
    ]
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
    _upsert(conn, _card_status_history, changes)


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
            "limit_id": limit.id,
            "taken_at": taken_at,
            "card_id": limit.card_id,
            "group_id": limit.group_id,
            "product": limit.product,
            "liters": limit.liters,
            "liters_used": limit.liters_used,
            "unit": limit.unit,
            "amount": limit.amount,
            "amount_used": limit.amount_used,
            "period_type": limit.period_type,
            "period_number": limit.period_number,
        }
        for limit in limits
    ]
    _upsert(conn, _limit_snapshots, rows)


def load_limits(conn: Connection, contract_key: str) -> list[Limit]:
    """Лимиты из последнего снимка договора."""
    table = _limit_snapshots
    latest = (
        select(func.max(table.c.taken_at))
        .where(table.c.contract_key == contract_key)
        .scalar_subquery()
    )
    query = (
        select(table)
        .where(table.c.contract_key == contract_key)
        .where(table.c.taken_at == latest)
        .order_by(table.c.limit_id)
    )
    return [
        Limit(
            id=row.limit_id,
            product=row.product,
            card_id=row.card_id,
            group_id=row.group_id,
            liters=row.liters,
            liters_used=row.liters_used,
            unit=row.unit,
            amount=row.amount,
            amount_used=row.amount_used,
            period_type=row.period_type,
            period_number=row.period_number,
        )
        for row in conn.execute(query)
    ]


def save_card_groups(
    conn: Connection, contract_key: str, groups: Iterable[CardGroup], now: datetime
) -> None:
    rows = [
        {
            "contract_key": contract_key,
            "id": group.id,
            "name": group.name,
            "cards_count": group.cards_count,
            "updated_at": now,
        }
        for group in groups
    ]
    _upsert(conn, _card_groups, rows)


def load_card_groups(conn: Connection, contract_key: str) -> list[CardGroup]:
    table = _card_groups
    query = select(table).where(table.c.contract_key == contract_key).order_by(table.c.id)
    return [CardGroup(row.id, row.name, row.cards_count) for row in conn.execute(query)]


def load_status_history(conn: Connection, contract_key: str, card_id: str) -> list[StatusChange]:
    table = _card_status_history
    query = (
        select(table)
        .where(table.c.contract_key == contract_key)
        .where(table.c.card_id == card_id)
        .order_by(table.c.seen_at)
    )
    return [
        StatusChange(row.card_id, row.seen_at, row.status, row.previous_status)
        for row in conn.execute(query)
    ]


def save_contract_snapshot(
    conn: Connection, contract_key: str, snapshot: ContractSnapshot, taken_at: datetime
) -> None:
    row = {"contract_key": contract_key, "taken_at": taken_at, **_fields(snapshot)}
    _upsert(conn, _contract_snapshots, [row])


def load_contract_snapshot(
    conn: Connection, contract_key: str
) -> tuple[datetime, ContractSnapshot] | None:
    """Последний снимок договора и момент, когда он снят."""
    table = _contract_snapshots
    query = (
        select(table)
        .where(table.c.contract_key == contract_key)
        .order_by(table.c.taken_at.desc())
        .limit(1)
    )
    row = conn.execute(query).first()
    if row is None:
        return None

    values = row._asdict()
    del values["contract_key"]
    taken_at = values.pop("taken_at")
    return taken_at, ContractSnapshot(**values)


def save_payments(
    conn: Connection, contract_key: str, payments: Iterable[Payment], now: datetime
) -> None:
    rows = [
        {"contract_key": contract_key, **_fields(payment), "loaded_at": now} for payment in payments
    ]
    _upsert(conn, _payments, rows)


def load_payments(
    conn: Connection, contract_key: str, date_from: date, date_to: date
) -> list[Payment]:
    """Платежи за период, обе даты включительно."""
    table = _payments
    query = (
        select(table)
        .where(table.c.contract_key == contract_key)
        .where(table.c.paid_at >= _day_start(date_from))
        .where(table.c.paid_at < _day_start(date_to + timedelta(days=1)))
        .order_by(table.c.paid_at, table.c.id)
    )
    return [_from_row(Payment, row) for row in conn.execute(query)]


def save_documents(
    conn: Connection, contract_key: str, documents: Iterable[Document], now: datetime
) -> None:
    rows = [
        {"contract_key": contract_key, **_fields(document), "loaded_at": now}
        for document in documents
    ]
    _upsert(conn, _documents, rows)


def load_documents(
    conn: Connection, contract_key: str, date_from: date, date_to: date
) -> list[Document]:
    """Документы с датой в периоде, обе даты включительно."""
    table = _documents
    query = (
        select(table)
        .where(table.c.contract_key == contract_key)
        .where(table.c.issued_at >= _day_start(date_from))
        .where(table.c.issued_at < _day_start(date_to + timedelta(days=1)))
        .order_by(table.c.issued_at, table.c.id)
    )
    return [_from_row(Document, row) for row in conn.execute(query)]


def count_cards(conn: Connection, contract_key: str) -> int:
    query = select(func.count()).select_from(_cards).where(_cards.c.contract_key == contract_key)
    return int(conn.execute(query).scalar_one())


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


_Record = TypeVar("_Record")


def _fields(record: object) -> dict[str, Any]:
    return {field.name: getattr(record, field.name) for field in fields(record)}  # type: ignore[arg-type]


def _from_row(cls: type[_Record], row: Any) -> _Record:
    return cls(**{field.name: getattr(row, field.name) for field in fields(cls)})  # type: ignore[arg-type]


def _day_start(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time())


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
