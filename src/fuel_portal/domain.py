"""Понятия предметной области портала: карта, АЗС, транзакция, лимит, договор.

Модель не зависит от SDK: адаптер синхронизации переводит в неё ответы API, а
аналитика и интерфейс работают только с ней. Поля повторяют смысл полей
apisdkopti24 (TransactionItemV2, CardV2Item, AzsItemV2, LimitItem, ContractResponse,
PaymentItem, DocumentItem, CardGroupItem).
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "Card",
    "CardGroup",
    "CardStatus",
    "ContractSnapshot",
    "Document",
    "Limit",
    "Payment",
    "Product",
    "ProductCategory",
    "Station",
    "StatusChange",
    "Transaction",
    "mask_card_number",
]

_VISIBLE_DIGITS = 4


class CardStatus(StrEnum):
    ACTIVE = "Active"
    BLOCKED = "Blocked"


class ProductCategory(StrEnum):
    FUEL = "fuel"
    GOODS = "goods"


class Product(StrEnum):
    AI92 = "АИ-92"
    AI95 = "АИ-95"
    DIESEL = "ДТ"
    COFFEE = "Кофе"
    WASHER = "Омывающая жидкость"

    @property
    def category(self) -> ProductCategory:
        if self in (Product.COFFEE, Product.WASHER):
            return ProductCategory.GOODS
        return ProductCategory.FUEL


@dataclass(frozen=True, slots=True)
class Station:
    id: str
    brand: str
    region: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class Card:
    id: str
    number: str
    status: str  # как в API; известные значения — CardStatus
    group: str | None = None
    comment: str | None = None
    product: str | None = None  # топливо карты; в API его нет, задаёт синтетика
    home_region: str | None = None  # в API нет, задаёт синтетика


@dataclass(frozen=True, slots=True)
class Transaction:
    id: str
    timestamp: datetime  # местное время АЗС, как поле timestamp в API
    card_id: str
    station_id: str
    product: str  # название как в API (product_name); у синтетики — значение Product
    qty: Decimal  # литры или штуки; у сторно отрицательные
    price: Decimal
    price_no_discount: Decimal
    amount: Decimal  # сумма со скидкой (поле sum в API)
    discount: Decimal
    is_storno: bool
    utc_time: datetime | None = None
    operation_type: str | None = None  # P — покупка, R — возврат
    amount_no_discount: Decimal | None = None
    storno_of: str | None = None  # ID отменённой операции


@dataclass(frozen=True, slots=True)
class Limit:
    """Продуктовый лимит карты или группы (LimitItem). Период — коды API как есть."""

    id: str
    product: str  # productType из API; у синтетики — значение Product
    card_id: str | None = None
    group_id: str | None = None
    liters: Decimal | None = None  # amount.value
    liters_used: Decimal | None = None  # amount.used
    unit: str | None = None  # amount.unit, например LIT
    amount: Decimal | None = None  # sum.value, ₽
    amount_used: Decimal | None = None  # sum.used, ₽
    period_type: int | None = None  # time.type
    period_number: int | None = None  # time.number


@dataclass(frozen=True, slots=True)
class StatusChange:
    """Смена статуса карты, замеченная синхронизацией (не точное время в API)."""

    card_id: str
    seen_at: datetime
    status: str
    previous_status: str | None  # None — карта появилась впервые


@dataclass(frozen=True, slots=True)
class CardGroup:
    id: str
    name: str
    cards_count: int


@dataclass(frozen=True, slots=True)
class ContractSnapshot:
    """Деньги и условия договора на момент запроса (get_contract_data).

    Этих чисел нет в операциях; по ним сверяются загруженные операции и платежи.
    """

    available: Decimal  # доступный остаток
    own_funds: Decimal  # собственные средства (own_balance)
    balance: Decimal  # собственные средства с учётом блокировок
    month_spent: Decimal  # расход текущего месяца, ₽
    month_liters: Decimal  # объём текущего месяца, л
    prev_month_liters: Decimal  # объём прошлого месяца, л: ступень сервисного сбора
    status: str
    pay_scheme: str  # условия оплаты, код API (PRE — предоплата)
    cards_total: int
    cards_active: int
    credit_limit: Decimal | None = None
    amount_limiter_used: Decimal | None = None  # накопленная сумма по ограничителю договора
    amount_limiter_left: Decimal | None = None  # доступно по ограничителю договора
    last_payment_amount: Decimal | None = None
    last_payment_date: date | None = None


@dataclass(frozen=True, slots=True)
class Payment:
    """Поступление денег на договор (PaymentItem)."""

    id: str
    paid_at: datetime
    amount: Decimal
    amount_client: Decimal
    payment_type: str
    payment_name: str
    number: str | None = None
    description: str | None = None  # текст банка: данные, а не указания


@dataclass(frozen=True, slots=True)
class Document:
    """Закрывающий документ (УПД и др.): итог месяца от оператора."""

    id: str
    name: str
    number: str
    issued_at: datetime  # из UNIX timestamp API, UTC
    total: Decimal  # с НДС
    vat: Decimal
    amount: Decimal  # без НДС


def mask_card_number(number: str) -> str:
    """Скрыть номер карты: «**** 1234». Номер карты — персональные данные."""
    return f"**** {number[-_VISIBLE_DIGITS:]}"
