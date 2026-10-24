from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine

from fuel_portal import storage
from fuel_portal.domain import Card, ContractSnapshot, Document, Payment, Transaction
from fuel_portal.reconcile import check_contract, check_documents

CONTRACT = "syn-contract-1"
TAKEN_AT = datetime(2026, 10, 10, 12, 0)

_SNAPSHOT = ContractSnapshot(
    available=Decimal("50000"),
    own_funds=Decimal("50000"),
    balance=Decimal("50000"),
    month_spent=Decimal("1500.50"),
    month_liters=Decimal("30"),
    prev_month_liters=Decimal("41000"),
    status="Active",
    pay_scheme="PRE",
    cards_total=2,
    cards_active=2,
    last_payment_amount=Decimal("40000"),
    last_payment_date=date(2026, 10, 1),
)


def _operation(op_id: str, moment: datetime, amount: str, qty: str) -> Transaction:
    return Transaction(
        id=op_id,
        timestamp=moment,
        card_id="syn-c-001",
        station_id="syn-s-01",
        product="ДТ",
        qty=Decimal(qty),
        price=Decimal("50"),
        price_no_discount=Decimal("52"),
        amount=Decimal(amount),
        discount=Decimal("0"),
        is_storno=False,
    )


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    url = f"sqlite:///{tmp_path / 'portal.db'}"
    storage.migrate(url)
    engine = storage.connect(url)
    cards = [
        Card("syn-c-001", "9999000000000001", "Active"),
        Card("syn-c-002", "9999000000000002", "Active"),
    ]
    operations = [
        _operation("1", datetime(2026, 9, 30, 23, 0), "999.99", "20"),  # прошлый месяц
        _operation("2", datetime(2026, 10, 2, 8, 0), "1000.50", "20"),
        _operation("3", datetime(2026, 10, 9, 18, 0), "500", "10"),
        _operation("4", datetime(2026, 10, 10, 13, 0), "300", "6"),  # после снимка
    ]
    payment = Payment(
        "syn-pay-1",
        datetime(2026, 10, 1, 10, 15),
        Decimal("40000"),
        Decimal("40000"),
        "P;Advice",
        "Payment To Client Contract",
    )
    with engine.begin() as conn:
        storage.save_cards(conn, CONTRACT, cards, TAKEN_AT)
        storage.save_transactions(conn, CONTRACT, operations, TAKEN_AT)
        storage.save_payments(conn, CONTRACT, [payment], TAKEN_AT)
    return engine


def test_contract_totals_match_loaded_operations(engine: Engine) -> None:
    with engine.connect() as conn:
        checks = check_contract(conn, CONTRACT, TAKEN_AT, _SNAPSHOT)

    assert [(check.name, check.matches) for check in checks] == [
        ("Расход месяца, ₽", True),
        ("Объём месяца", True),
        ("Карт в договоре", True),
        ("Последний платёж, ₽", True),
    ]


def test_missing_operations_show_as_difference(engine: Engine) -> None:
    snapshot = replace(_SNAPSHOT, month_spent=Decimal("1800.50"), cards_total=3)

    with engine.connect() as conn:
        spent, _, cards, _ = check_contract(conn, CONTRACT, TAKEN_AT, snapshot)

    assert spent.difference == Decimal("300")
    assert cards.difference == Decimal("1")


def test_document_is_compared_with_its_month(engine: Engine) -> None:
    document = Document(
        "syn-doc-1",
        "УПД",
        "SYN0000001",
        datetime(2026, 9, 30, 20, 59, 59),
        total=Decimal("1037.49"),
        vat=Decimal("172.92"),
        amount=Decimal("864.57"),
    )
    with engine.begin() as conn:
        storage.save_documents(conn, CONTRACT, [document], TAKEN_AT)

    with engine.connect() as conn:
        (check,) = check_documents(conn, CONTRACT, date(2026, 9, 1), date(2026, 9, 30))

    assert (check.api_value, check.db_value) == (Decimal("1037.49"), Decimal("999.99"))
    assert check.difference == Decimal("37.50")  # например, сервисный сбор
