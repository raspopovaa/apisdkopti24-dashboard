"""Понятия предметной области портала: карта, АЗС, транзакция, лимит.

Модель не зависит от SDK: адаптер синхронизации переводит в неё ответы API, а
аналитика и интерфейс работают только с ней. Поля повторяют смысл полей
apisdkopti24 (TransactionItemV2, CardV2Item, AzsItemV2, LimitItem).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "Card",
    "CardStatus",
    "Limit",
    "Product",
    "ProductCategory",
    "Station",
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
    group: str
    status: CardStatus
    product: Product
    home_region: str


@dataclass(frozen=True, slots=True)
class Transaction:
    id: str
    timestamp: datetime  # местное время АЗС, как поле timestamp в API
    card_id: str
    station_id: str
    product: Product
    qty: Decimal  # литры или штуки; у сторно отрицательные
    price: Decimal
    price_no_discount: Decimal
    amount: Decimal  # сумма со скидкой (поле sum в API)
    discount: Decimal
    is_storno: bool


@dataclass(frozen=True, slots=True)
class Limit:
    card_id: str
    product: Product
    liters_per_month: Decimal


def mask_card_number(number: str) -> str:
    """Скрыть номер карты: «**** 1234». Номер карты — персональные данные."""
    return f"**** {number[-_VISIBLE_DIGITS:]}"
