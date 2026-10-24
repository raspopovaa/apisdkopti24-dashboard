"""Сверка загруженных операций с итогами договора из API.

Операции в базе — сумма отдельных строк; снимок договора, платежи и закрывающие
документы — итоги, которые оператор считает сам. Расхождение значит, что
синхронизация что-то пропустила или оператор начислил то, чего нет в операциях
(сервисный сбор, корректировки). Сверка показывает разницу, а не её причину.

    storage ──► reconcile ──► интерфейс, агент
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Connection

from . import storage
from .domain import ContractSnapshot, Document, Transaction

__all__ = ["Check", "check_contract", "check_documents"]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    api_value: Decimal
    db_value: Decimal
    note: str = ""

    @property
    def difference(self) -> Decimal:
        """API минус база: больше нуля — в базе не хватает."""
        return self.api_value - self.db_value

    @property
    def matches(self) -> bool:
        return self.difference == 0


def check_contract(
    conn: Connection, contract_key: str, taken_at: datetime, snapshot: ContractSnapshot
) -> list[Check]:
    """Сверить снимок договора с операциями, картами и платежами в базе."""
    month_start = taken_at.date().replace(day=1)
    month = storage.load_transactions(conn, contract_key, month_start, taken_at.date())
    month = [item for item in month if item.timestamp <= taken_at]
    checks = [
        Check(
            "Расход месяца, ₽",
            snapshot.month_spent,
            _amount(month),
            "consumption_for_month против суммы операций с 1-го числа до снимка",
        ),
        Check(
            "Объём месяца",
            snapshot.month_liters,
            sum((item.qty for item in month), Decimal(0)),
            "consumption_for_month_volume против qty операций; штучные товары тоже в qty",
        ),
        Check(
            "Карт в договоре",
            Decimal(snapshot.cards_total),
            Decimal(storage.count_cards(conn, contract_key)),
            "cards_quantity_all против карт из get_cards_v2",
        ),
    ]
    if snapshot.last_payment_amount is not None and snapshot.last_payment_date is not None:
        day = snapshot.last_payment_date
        paid = storage.load_payments(conn, contract_key, day, day)
        checks.append(
            Check(
                "Последний платёж, ₽",
                snapshot.last_payment_amount,
                sum((item.amount for item in paid), Decimal(0)),
                f"last_payment_sum против платежей за {day.isoformat()}",
            )
        )
    return checks


def check_documents(
    conn: Connection, contract_key: str, date_from: date, date_to: date
) -> list[Check]:
    """Сверить закрывающие документы периода с операциями их месяца.

    Документ с суммой с НДС сравнивается с суммой операций месяца даты документа.
    Сервисный сбор в операциях не выделен, поэтому документ обычно больше.
    """
    checks: list[Check] = []
    for document in storage.load_documents(conn, contract_key, date_from, date_to):
        first, last = _month(document)
        month = storage.load_transactions(conn, contract_key, first, last)
        checks.append(
            Check(
                f"{document.name} {document.number}, ₽",
                document.total,
                _amount(month),
                f"итог документа с НДС против операций {first:%m.%Y}",
            )
        )
    return checks


def _amount(items: Iterable[Transaction]) -> Decimal:
    # Сторно приходят с суммой со знаком минус: простая сумма уже их вычитает.
    return sum((item.amount for item in items), Decimal(0))


def _month(document: Document) -> tuple[date, date]:
    first = document.issued_at.date().replace(day=1)
    next_month = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return first, date.fromordinal(next_month.toordinal() - 1)
