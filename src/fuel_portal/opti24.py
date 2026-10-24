"""Адаптер SDK apisdkopti24: ответы API → понятия предметной области.

Единственное место, где портал вызывает SDK. Остальной код видит только
Card и Transaction из domain.py.
"""

import math
from datetime import UTC, date, datetime
from decimal import Decimal

from apisdkopti24 import APIClient
from apisdkopti24.models.cards import CardV2Item
from apisdkopti24.models.transactions import TransactionItemV2

from .domain import Card, Transaction

__all__ = ["PAGE_SIZE", "fetch_cards", "fetch_transactions"]

PAGE_SIZE = 100  # операций на странице; страница get_transactions_v2 = 1 запрос пакета


async def fetch_cards(client: APIClient, contract_id: str) -> list[Card]:
    """Все карты договора. get_cards_v2 не тарифицируется."""
    cards = client.cards.iter_cards_v2(contract_id=contract_id, strict=True)
    return [_card(item) async for item in cards]


async def fetch_transactions(
    client: APIClient, contract_id: str, date_from: date, date_to: date, max_pages: int
) -> tuple[list[Transaction], int]:
    """Операции договора за период не длиннее месяца и число тарифицируемых страниц.

    strict=True: если страниц больше max_pages, перебор падает, а не отдаёт часть.
    Число страниц — оценка по числу операций: пустой ответ тоже стоит запрос.
    """
    pages = client.transactions.iter_transactions_v2(
        contract_id=contract_id,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        page_limit=PAGE_SIZE,
        max_pages=max_pages,
        strict=True,
    )
    items = [_transaction(item) async for item in pages]
    return items, max(1, math.ceil(len(items) / PAGE_SIZE))


def _card(item: CardV2Item) -> Card:
    return Card(
        id=item.id,
        number=item.number,
        status=item.status,
        group=item.group_name,
        comment=item.comment,
    )


def _transaction(item: TransactionItemV2) -> Transaction:
    # timestamp — местное время АЗС, хотя строка оканчивается на Z: пояс отбрасываем.
    storno_of = None if item.stor_transaction_id is None else str(item.stor_transaction_id)
    return Transaction(
        id=str(item.id),
        timestamp=item.timestamp.replace(tzinfo=None),
        card_id=item.card_id,
        station_id=item.poi_id,
        product=item.product_name,
        qty=_decimal(item.qty),
        price=_decimal(item.price),
        price_no_discount=_decimal(item.price_no_discount),
        amount=_decimal(item.sum),
        discount=_decimal(item.discount),
        is_storno=item.is_storno,
        utc_time=_utc(item.utc_time),
        operation_type=item.type,
        amount_no_discount=_decimal(item.sum_no_discount),
        storno_of=storno_of,
    )


def _decimal(value: float | str) -> Decimal:
    # Через str: Decimal(0.1) из float дал бы 0.1000000000000000055…
    return Decimal(str(value))


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment

    return moment.astimezone(UTC).replace(tzinfo=None)
