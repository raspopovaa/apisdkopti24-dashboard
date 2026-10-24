"""Синхронизация договора: API ОПТИ 24 → хранилище.

    sync_contract ──► opti24 (SDK) ──► API
          │
          └────────► storage (одна транзакция БД)

Прогон: карты (бесплатно), затем операции от последней загрузки с нахлёстом,
по месяцам (API не отдаёт период длиннее месяца). Сначала всё читается из API,
потом пишется одной транзакцией: сбой посередине не сдвигает отметку загрузки.
"""

import calendar
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from apisdkopti24 import APIClient
from sqlalchemy import Engine

from . import opti24, storage
from .domain import Transaction

__all__ = ["DEFAULT_OVERLAP", "Source", "SyncReport", "sync_contract"]

# Сторно и ручные корректировки приходят позже операции: перечитываем последние дни.
DEFAULT_OVERLAP = timedelta(days=3)
_DEFAULT_MAX_PAGES = 20  # на месяц: 2 000 операций, больше — заказ отчёта


class Source(StrEnum):
    CARDS = "cards"
    TRANSACTIONS = "transactions"


@dataclass(frozen=True, slots=True)
class SyncReport:
    cards: int
    transactions: int
    billable_calls: int
    date_from: date
    date_to: date


async def sync_contract(
    client: APIClient,
    engine: Engine,
    *,
    contract_key: str,
    contract_id: str,
    now: datetime,
    first_day: date | None = None,
    overlap: timedelta = DEFAULT_OVERLAP,
    max_pages: int = _DEFAULT_MAX_PAGES,
) -> SyncReport:
    """Загрузить карты и новые операции договора.

    first_day — с какого дня грузить, если договор ещё не загружался;
    по умолчанию — первое число текущего месяца. Историю за прошлые месяцы
    дешевле загрузить транзакционным отчётом.
    """
    with engine.connect() as conn:
        state = storage.load_sync_state(conn, contract_key, Source.TRANSACTIONS)

    date_from = _start(state, now, first_day, overlap)
    date_to = now.date()

    cards = await opti24.fetch_cards(client, contract_id)
    transactions: list[Transaction] = []
    billable = 0
    for chunk_from, chunk_to in _months(date_from, date_to):
        items, pages = await opti24.fetch_transactions(
            client, contract_id, chunk_from, chunk_to, max_pages
        )
        transactions.extend(items)
        billable += pages

    with engine.begin() as conn:
        storage.save_cards(conn, contract_key, cards, now)
        storage.save_transactions(conn, contract_key, transactions, now)
        storage.save_sync_state(conn, contract_key, storage.SyncState(Source.CARDS, now, now, 0))
        storage.save_sync_state(
            conn, contract_key, storage.SyncState(Source.TRANSACTIONS, now, now, billable)
        )

    return SyncReport(len(cards), len(transactions), billable, date_from, date_to)


def _start(
    state: storage.SyncState | None,
    now: datetime,
    first_day: date | None,
    overlap: timedelta,
) -> date:
    if state is not None and state.loaded_until is not None:
        return (state.loaded_until - overlap).date()

    if first_day is not None:
        return first_day

    return now.date().replace(day=1)


def _months(date_from: date, date_to: date) -> Iterator[tuple[date, date]]:
    """Разбить период по календарным месяцам: 28.09–02.10 → 28.09–30.09, 01.10–02.10."""
    start = date_from
    while start <= date_to:
        last = start.replace(day=calendar.monthrange(start.year, start.month)[1])
        end = min(last, date_to)
        yield start, end
        start = end + timedelta(days=1)
