"""Синхронизация договора: API ОПТИ 24 → хранилище.

    sync_contract ──► opti24 (SDK) ──► API
          │
          └────────► storage (одна транзакция БД)

Прогон: карты, группы и лимиты (бесплатно), операции от последней загрузки
с нахлёстом по месяцам (API не отдаёт период длиннее месяца), закрывающие
документы за тот же период (бесплатно, на DEMO нет). По флагу with_money —
снимок денег договора и платежи (2 тарифицируемых запроса).
Сначала всё читается из API, потом пишется одной транзакцией: сбой посередине
не сдвигает отметку загрузки.
"""

import calendar
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from apisdkopti24 import APIClient
from sqlalchemy import Engine

from . import opti24, storage
from .domain import ContractSnapshot, Document, Payment, Transaction

__all__ = ["DEFAULT_OVERLAP", "Source", "SyncReport", "sync_contract"]

# Сторно и ручные корректировки приходят позже операции: перечитываем последние дни.
DEFAULT_OVERLAP = timedelta(days=3)
_DEFAULT_MAX_PAGES = 20  # на месяц: 2 000 операций, больше — заказ отчёта


_MONEY_CALLS = 2  # get_contract_data + get_payments


class Source(StrEnum):
    CARDS = "cards"
    TRANSACTIONS = "transactions"
    LIMITS = "limits"
    DOCUMENTS = "documents"
    MONEY = "money"


@dataclass(frozen=True, slots=True)
class SyncReport:
    cards: int
    transactions: int
    billable_calls: int
    date_from: date
    date_to: date
    limits: int = 0
    card_groups: int = 0
    documents: int = 0
    payments: int = 0
    contract_snapshot: bool = False


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
    with_documents: bool = True,
    with_money: bool = False,
) -> SyncReport:
    """Загрузить карты и новые операции договора.

    first_day — с какого дня грузить, если договор ещё не загружался;
    по умолчанию — первое число текущего месяца. Историю за прошлые месяцы
    дешевле загрузить транзакционным отчётом.
    with_documents=False — для DEMO, где get_documents нет.
    with_money=True — баланс и платежи, +2 тарифицируемых запроса за прогон.
    """
    with engine.connect() as conn:
        state = storage.load_sync_state(conn, contract_key, Source.TRANSACTIONS)

    date_from = _start(state, now, first_day, overlap)
    date_to = now.date()

    cards = await opti24.fetch_cards(client, contract_id)
    groups = await opti24.fetch_card_groups(client, contract_id)
    limits = await opti24.fetch_limits(client, contract_id)
    transactions: list[Transaction] = []
    billable = 0
    for chunk_from, chunk_to in _months(date_from, date_to):
        items, pages = await opti24.fetch_transactions(
            client, contract_id, chunk_from, chunk_to, max_pages
        )
        transactions.extend(items)
        billable += pages

    documents: list[Document] = []
    if with_documents:
        documents = await opti24.fetch_documents(client, contract_id, date_from, date_to)

    contract: ContractSnapshot | None = None
    payments: list[Payment] = []
    if with_money:
        contract = await opti24.fetch_contract(client, contract_id)
        payments = await opti24.fetch_payments(client, contract_id)

    with engine.begin() as conn:
        storage.save_cards(conn, contract_key, cards, now)
        storage.save_card_groups(conn, contract_key, groups, now)
        storage.save_limits(conn, contract_key, limits, now)
        storage.save_transactions(conn, contract_key, transactions, now)
        storage.save_documents(conn, contract_key, documents, now)
        states = [
            storage.SyncState(Source.CARDS, now, now, 0),
            storage.SyncState(Source.LIMITS, now, now, 0),
            storage.SyncState(Source.TRANSACTIONS, now, now, billable),
        ]
        if with_documents:
            states.append(storage.SyncState(Source.DOCUMENTS, now, now, 0))
        if contract is not None:
            storage.save_contract_snapshot(conn, contract_key, contract, now)
            storage.save_payments(conn, contract_key, payments, now)
            states.append(storage.SyncState(Source.MONEY, now, now, _MONEY_CALLS))
        for state in states:
            storage.save_sync_state(conn, contract_key, state)

    return SyncReport(
        cards=len(cards),
        transactions=len(transactions),
        billable_calls=billable + (_MONEY_CALLS if with_money else 0),
        date_from=date_from,
        date_to=date_to,
        limits=len(limits),
        card_groups=len(groups),
        documents=len(documents),
        payments=len(payments),
        contract_snapshot=contract is not None,
    )


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
