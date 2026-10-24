"""Адаптер SDK apisdkopti24: ответы API → понятия предметной области.

Единственное место, где портал вызывает SDK. Остальной код видит только
понятия из domain.py.

Стоимость (пакет запросов договора): get_cards_v2, get_limits, get_card_groups,
get_documents — бесплатно; get_transactions_v2 — запрос за страницу;
get_contract_data и get_payments — по одному запросу.
"""

import math
from datetime import UTC, date, datetime
from decimal import Decimal

from apisdkopti24 import APIClient
from apisdkopti24.models.card_group import CardGroupItem
from apisdkopti24.models.cards import CardV2Item
from apisdkopti24.models.contracts import ContractResponse, DocumentItem, PaymentItem
from apisdkopti24.models.limits import LimitItem
from apisdkopti24.models.transactions import TransactionItemV2

from .domain import (
    Card,
    CardGroup,
    ContractSnapshot,
    Document,
    Limit,
    Payment,
    Transaction,
)

__all__ = [
    "PAGE_SIZE",
    "fetch_card_groups",
    "fetch_cards",
    "fetch_contract",
    "fetch_documents",
    "fetch_limits",
    "fetch_payments",
    "fetch_transactions",
]

PAGE_SIZE = 100  # операций на странице; страница get_transactions_v2 = 1 запрос пакета
_DOCUMENTS_PAGE = 50
_DOCUMENTS_MAX_PAGES = 10


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


async def fetch_contract(client: APIClient, contract_id: str) -> ContractSnapshot:
    """Деньги и условия договора. Тарифицируется: 1 запрос."""
    response = await client.contracts.get_contract_data(contract_id=contract_id)
    return _contract(response.data)


async def fetch_payments(client: APIClient, contract_id: str) -> list[Payment]:
    """Платежи договора. Тарифицируется: 1 запрос; периода у метода нет."""
    response = await client.contracts.get_payments(contract_id=contract_id)
    return [_payment(item) for item in response.data.result or []]


async def fetch_limits(client: APIClient, contract_id: str) -> list[Limit]:
    """Все продуктовые лимиты договора. get_limits не тарифицируется."""
    response = await client.limits.get_limits(contract_id=contract_id)
    return [_limit(item) for item in response.data.result or []]


async def fetch_card_groups(client: APIClient, contract_id: str) -> list[CardGroup]:
    """Группы карт. get_card_groups не тарифицируется."""
    response = await client.card_groups.get_card_groups(contract_id=contract_id)
    return [_card_group(item) for item in response.data.result or []]


async def fetch_documents(
    client: APIClient, contract_id: str, date_from: date, date_to: date
) -> list[Document]:
    """Закрывающие документы за период. get_documents не тарифицируется; на DEMO нет."""
    documents: list[Document] = []
    for page in range(1, _DOCUMENTS_MAX_PAGES + 1):
        response = await client.contracts.get_documents(
            contract_id=contract_id,
            date_start=date_from.isoformat(),
            date_end=date_to.isoformat(),
            page=page,
            on_page=_DOCUMENTS_PAGE,
        )
        items = response.data.result or []
        documents.extend(_document(item) for item in items)
        if len(documents) >= response.data.total_count or not items:
            return documents

    raise RuntimeError(f"Документов больше {_DOCUMENTS_MAX_PAGES * _DOCUMENTS_PAGE}: сузьте период")


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


def _contract(data: ContractResponse) -> ContractSnapshot:
    money = data.balanceData
    terms = data.contractData
    return ContractSnapshot(
        available=_decimal(money.available_amount),
        own_funds=_decimal(money.own_balance),
        balance=_decimal(money.balance),
        month_spent=_decimal(money.consumption_for_month),
        month_liters=_decimal(money.consumption_for_month_volume),
        prev_month_liters=_decimal(money.consumption_for_prev_month_volume),
        status=terms.contract_status,
        pay_scheme=terms.pay_scheme,
        cards_total=int(data.cardsData.cards_quantity_all),
        cards_active=int(data.cardsData.cards_quantity_active),
        credit_limit=_optional_decimal(terms.credit_limit),
        amount_limiter_used=_optional_decimal(terms.current_amount_limiter),
        amount_limiter_left=_optional_decimal(terms.balance_amount_limiter),
        last_payment_amount=_optional_decimal(money.last_payment_sum),
        last_payment_date=date.fromisoformat(money.last_payment_date)
        if money.last_payment_date
        else None,
    )


def _payment(item: PaymentItem) -> Payment:
    return Payment(
        id=item.id,
        paid_at=datetime.fromisoformat(item.date),
        amount=_decimal(item.amount),
        amount_client=_decimal(item.amount_client),
        payment_type=item.payment_type,
        payment_name=item.payment_name,
        number=item.payment_number,
        description=item.description.strip() or None,
    )


def _limit(item: LimitItem) -> Limit:
    return Limit(
        id=item.id,
        product=item.productType,
        card_id=item.card_id,
        group_id=item.group_id,
        liters=None if item.amount is None else _decimal(item.amount.value),
        liters_used=None if item.amount is None else _decimal(item.amount.used),
        unit=None if item.amount is None else item.amount.unit,
        amount=None if item.sum is None else _decimal(item.sum.value),
        amount_used=None if item.sum is None else _decimal(item.sum.used),
        period_type=item.time.type,
        period_number=item.time.number,
    )


def _card_group(item: CardGroupItem) -> CardGroup:
    return CardGroup(id=item.id, name=item.name, cards_count=int(item.cards_count))


def _document(item: DocumentItem) -> Document:
    return Document(
        id=item.id,
        name=item.name,
        number=item.number,
        issued_at=datetime.fromtimestamp(item.date, UTC).replace(tzinfo=None),
        total=_decimal(item.total),
        vat=_decimal(item.vat),
        amount=_decimal(item.sum),
    )


def _optional_decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value else None


def _decimal(value: float | str) -> Decimal:
    # Через str: Decimal(0.1) из float дал бы 0.1000000000000000055…
    return Decimal(str(value))


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment

    return moment.astimezone(UTC).replace(tzinfo=None)
