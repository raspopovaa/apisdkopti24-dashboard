import asyncio
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from apisdkopti24 import APIClient, AsyncTransport, ConnectionSettings, RateLimitPolicy
from apisdkopti24.credentials import StaticCredentialsProvider
from apisdkopti24.transport import AsyncHTTPClient
from sqlalchemy import Engine

from fuel_portal import storage
from fuel_portal.sync import SyncReport, sync_contract

BASE_URL = "https://api.example.test/vip/"
CONTRACT_ID = "syn-1-AAAAAAAA"
CONTRACT_KEY = "syn-contract-1"
NOW = datetime(2026, 10, 2, 9, 0)
_UNTHROTTLED = RateLimitPolicy(requests_per_second=10_000.0)


def _envelope(data: Any) -> dict[str, Any]:
    return {"status": {"code": 200}, "data": data, "timestamp": 1759500000}


_AUTH = {
    "session_id": "syn-session",
    "client_id": "syn-client",
    "client_status": "Active",
    "org_name": "Синтетика",
    "user_id": "syn-user",
    "contracts": [
        {"id": CONTRACT_ID, "number": "SYN-1", "mpc": False, "cards_count": 1, "one_price": False}
    ],
    "role_id": "Supervisor",
    "role_name": "Administrator",
    "access": {"web": True, "api": True, "mobile": True},
    "email": "user@example.test",
    "read_only": False,
}

_CARD = {
    "id": "syn-c-001",
    "group_id": "syn-g-1",
    "group_name": "Магистраль",
    "contract_id": CONTRACT_ID,
    "contract_name": "Синтетика",
    "number": "9999000000000001",
    "status": "Active",
    "status_name": "Активна",
    "comment": "Фура 1",
    "product": "limit",
    "product_name": "Лимитная",
    "carrier": "Plastic",
    "carrier_name": "Пластик",
    "platon": False,
    "avtodor": False,
    "sync_group_state": None,
    "users": None,
    "mpc": None,
}


def _operation(op_id: int, day: str, **extra: Any) -> dict[str, Any]:
    item = {
        "id": op_id,
        "timestamp": f"{day}T23:30:00.000000Z",  # местное время АЗС
        "utc_time": f"{day}T16:30:00.000000Z",
        "card_id": "syn-c-001",
        "poi_id": "syn-s-01",
        "terminal_id": "syn-term",
        "type": "P",
        "product_id": "syn-p-dt",
        "product_name": "ДТ-Е-К5 ОПТИ",
        "product_category_id": "НП",
        "currency": "RUR",
        "check_id": op_id,
        "stor_transaction_id": None,
        "is_storno": False,
        "is_manual_corrention": False,
        "qty": 150.25,
        "price": "63.17",
        "price_no_discount": 65.1,
        "sum": "9491.29",
        "sum_no_discount": 9781.28,
        "discount": 289.99,
        "exchange_rate": 1,
        "card_number": "9999000000000001",
        "payment_type": "Карта",
    }
    item.update(extra)
    return item


_GROUP = {
    "id": "syn-g-1",
    "name": "Магистраль",
    "cards_count": "1",
    "status": "Synchronize",
    "contract_id": CONTRACT_ID,
}

_LIMIT = {
    "id": "syn-l-1",
    "card_id": "syn-c-001",
    "group_id": None,
    "contract_id": CONTRACT_ID,
    "amount": {"value": 600, "used": 150.25, "unit": "LIT"},
    "productGroup": "syn-pg-fuel",
    "productType": "syn-pt-dt",
    "sum": None,
    "term": None,
    "transactions": None,
    "time": {"number": 1, "type": 7},
    "date": "01/10/2026 00:00:00",
}

_DOCUMENT = {
    "id": "syn-doc-1",
    "name": "УПД",
    "name_doc": "СчетФактураВыданный",
    "number": "SYN0000001",
    "date": 1790801999,  # 2026-09-30 23:59:59 МСК
    "total": 10000.5,
    "vat": 1666.75,
    "sum": 8333.75,
    "currency": "руб.",
    "consignee": "Синтетика",
    "contract_id": CONTRACT_ID,
    "contract_name": "SYN-1",
}

_CONTRACT = {
    "mpc": False,
    "template_id": "syn-t-1",
    "status": "Active",
    "status_crm": "Active",
    "Is_dealer": False,
    "balanceData": {
        "available_amount": "63363.02",
        "own_balance": "63363.02",
        "balance": "61363.02",
        "consumption_for_month": "9491.29",
        "consumption_for_month_volume": "150.25",
        "consumption_for_prev_month_volume": "41000",
        "last_payment_sum": "40000",
        "last_payment_date": "2026-10-01",
        "currency": "810",
    },
    "contractData": {
        "contract_id": CONTRACT_ID,
        "way_id": "syn",
        "contract_number": "SYN-1",
        "unique_payment_id": "syn",
        "client": "syn-client",
        "client_category": "Commercial",
        "contract_category": "SYN-1",
        "country": "RUS",
        "region": "77",
        "fin_institution": "Синтетика",
        "invoice_scheme": "ON",
        "invoice_period": "",
        "invoice_pmt_delay": "14",
        "contract_status": "Active",
        "contract_status_name": "Активен",
        "pay_scheme": "PRE",
        "discount_scheme ": "TRF_APPLY_Y",
        "auto_pay": "0",
        "auto_pay_type": "",
        "credit_limit": None,
        "current_amount_limiter": "500000.1",
        "balance_amount_limiter": "499999.9",
        "max_amount_limiter": "1000000",
        "date_open": "2026-01-01",
        "effective_date": "2026-01-01",
        "end_date": "2030-01-01",
        "date_expire": "",
        "product_type ": True,
        "type_code": "",
        "supplier_name": "Синтетика",
    },
    "cardsData": {
        "cards_quantity_all": "1",
        "cards_quantity_active": "1",
        "card_groups_quantity_all": "1",
    },
    "managerData": {
        "email": "manager@example.test",
        "first_name": "Синт",
        "last_name": "Менеджер",
        "middle_name": None,
        "work_phone": None,
    },
}

_PAYMENT = {
    "id": "syn-pay-1",
    "contract_id": CONTRACT_ID,
    "date": "2026-10-01T10:15:00",
    "amount": "40000",
    "currency": "810;RUR",
    "amount_client": "40000",
    "description": " Платёж SYN от 01.10.2026 ",
    "payment_name": "Payment To Client Contract",
    "payment_type": "P;Advice",
    "payment_number": "SYN0001",
}


class _FakeAPI:
    """Подставной API: операции по дням; запоминает пути и периоды запросов."""

    def __init__(self, operations: list[dict[str, Any]]) -> None:
        self.operations = operations
        self.periods: list[tuple[str, str]] = []
        self.paths: list[str] = []
        self.card_status = "Active"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path.rsplit("/", 2)[-1])
        if path.endswith("/v1/authUser"):
            return httpx.Response(200, json=_envelope(_AUTH), request=request)

        if path.endswith("/v2/cards"):
            card = {**_CARD, "status": self.card_status}
            return _listed(request, [card])

        if path.endswith("/v1/cardGroups"):
            return _listed(request, [_GROUP])

        if path.endswith("/v1/limit"):
            return _listed(request, [_LIMIT])

        if path.endswith("/v2/documents"):
            return _listed(request, [_DOCUMENT])

        if path.endswith("/v1/getPartContractData"):
            return httpx.Response(200, json=_envelope(_CONTRACT), request=request)

        if path.endswith("/v1/getPayments"):
            return _listed(request, [_PAYMENT])

        if path.endswith("/v2/transactions"):
            date_from = request.url.params["date_from"]
            date_to = request.url.params["date_to"]
            self.periods.append((date_from, date_to))
            found = [
                item for item in self.operations if date_from <= item["timestamp"][:10] <= date_to
            ]
            data = {"total_count": len(found), "result": found}
            return httpx.Response(200, json=_envelope(data), request=request)

        raise AssertionError(f"Неожиданный запрос: {request.url}")


def _listed(request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
    data = {"total_count": len(items), "result": items}
    return httpx.Response(200, json=_envelope(data), request=request)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    url = f"sqlite:///{tmp_path / 'portal.db'}"
    storage.migrate(url)
    return storage.connect(url)


def _run(api: _FakeAPI, engine: Engine, tmp_path: Path, now: datetime, **kwargs: Any) -> SyncReport:
    async def scenario() -> SyncReport:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(api))
        # httpx.AsyncClient подходит SDK, но mypy не сводит его сигнатуры к протоколу.
        transport = AsyncTransport(
            BASE_URL,
            http_client=cast(AsyncHTTPClient, http_client),
            rate_limit_policy=_UNTHROTTLED,
        )
        settings = ConnectionSettings(
            base_url=BASE_URL,
            logger_file=str(tmp_path / "sdk.log"),
            request_log_file=str(tmp_path / "requests.jsonl"),
        )
        credentials = StaticCredentialsProvider(api_key="key", login="demo", password="password")
        async with APIClient(
            settings=settings, credentials_provider=credentials, transport=transport
        ) as client:
            await client.auth.auth_user()
            report = await sync_contract(
                client,
                engine,
                contract_key=CONTRACT_KEY,
                contract_id=CONTRACT_ID,
                now=now,
                **kwargs,
            )
        await http_client.aclose()
        return report

    return asyncio.run(scenario())


def test_first_sync_splits_period_by_month(engine: Engine, tmp_path: Path) -> None:
    api = _FakeAPI([_operation(1, "2026-09-29"), _operation(2, "2026-10-01")])

    report = _run(api, engine, tmp_path, NOW, first_day=date(2026, 9, 28))

    assert api.periods == [("2026-09-28", "2026-09-30"), ("2026-10-01", "2026-10-02")]
    assert (report.cards, report.transactions, report.billable_calls) == (1, 2, 2)


def test_api_values_are_stored_exactly(engine: Engine, tmp_path: Path) -> None:
    api = _FakeAPI([_operation(7, "2026-10-01", stor_transaction_id=5, is_storno=True)])

    _run(api, engine, tmp_path, NOW)

    with engine.connect() as conn:
        (card,) = storage.load_cards(conn, CONTRACT_KEY)
        (item,) = storage.load_transactions(
            conn, CONTRACT_KEY, date(2026, 10, 1), date(2026, 10, 1)
        )

    assert (card.status, card.group, card.comment) == ("Active", "Магистраль", "Фура 1")
    assert item.id == "7"
    assert item.timestamp == datetime(2026, 10, 1, 23, 30)
    assert item.utc_time == datetime(2026, 10, 1, 16, 30)
    assert item.product == "ДТ-Е-К5 ОПТИ"
    assert (item.qty, item.price, item.amount) == (
        Decimal("150.25"),
        Decimal("63.17"),
        Decimal("9491.29"),
    )
    assert (item.amount_no_discount, item.discount) == (Decimal("9781.28"), Decimal("289.99"))
    assert (item.is_storno, item.storno_of, item.operation_type) == (True, "5", "P")


def test_next_sync_starts_from_last_load_with_overlap(engine: Engine, tmp_path: Path) -> None:
    api = _FakeAPI([_operation(1, "2026-10-01")])
    _run(api, engine, tmp_path, NOW)
    api.periods.clear()
    api.operations.append(_operation(2, "2026-10-09"))

    report = _run(api, engine, tmp_path, datetime(2026, 10, 10, 9, 0))

    assert api.periods == [("2026-09-29", "2026-09-30"), ("2026-10-01", "2026-10-10")]
    with engine.connect() as conn:
        stored = storage.load_transactions(
            conn, CONTRACT_KEY, date(2026, 10, 1), date(2026, 10, 31)
        )
    assert [item.id for item in stored] == ["1", "2"]
    assert report.transactions == 2


def test_free_parameters_are_stored(engine: Engine, tmp_path: Path) -> None:
    api = _FakeAPI([_operation(1, "2026-10-01")])

    report = _run(api, engine, tmp_path, NOW, first_day=date(2026, 9, 28))

    with engine.connect() as conn:
        (limit,) = storage.load_limits(conn, CONTRACT_KEY)
        (group,) = storage.load_card_groups(conn, CONTRACT_KEY)
        (document,) = storage.load_documents(conn, CONTRACT_KEY, date(2026, 9, 1), NOW.date())

    assert (limit.card_id, limit.product, limit.unit) == ("syn-c-001", "syn-pt-dt", "LIT")
    assert (limit.liters, limit.liters_used) == (Decimal("600"), Decimal("150.25"))
    assert (limit.period_type, limit.period_number) == (7, 1)
    assert (group.name, group.cards_count) == ("Магистраль", 1)
    assert (document.number, document.total, document.vat) == (
        "SYN0000001",
        Decimal("10000.5"),
        Decimal("1666.75"),
    )
    assert document.issued_at == datetime(2026, 9, 30, 20, 59, 59)  # UTC
    assert (report.limits, report.card_groups, report.documents) == (1, 1, 1)
    assert "getPartContractData" not in api.paths
    assert "getPayments" not in api.paths


def test_money_sync_stores_balance_and_payments(engine: Engine, tmp_path: Path) -> None:
    api = _FakeAPI([_operation(1, "2026-10-01")])

    report = _run(api, engine, tmp_path, NOW, with_money=True)

    with engine.connect() as conn:
        loaded = storage.load_contract_snapshot(conn, CONTRACT_KEY)
        (payment,) = storage.load_payments(conn, CONTRACT_KEY, date(2026, 10, 1), date(2026, 10, 1))

    assert loaded is not None
    taken_at, snapshot = loaded
    assert taken_at == NOW
    assert (snapshot.available, snapshot.balance) == (Decimal("63363.02"), Decimal("61363.02"))
    assert snapshot.prev_month_liters == Decimal("41000")
    assert (snapshot.credit_limit, snapshot.amount_limiter_left) == (None, Decimal("499999.9"))
    assert snapshot.last_payment_date == date(2026, 10, 1)
    assert (payment.amount, payment.paid_at) == (Decimal("40000"), datetime(2026, 10, 1, 10, 15))
    assert payment.description == "Платёж SYN от 01.10.2026"
    assert report.billable_calls == 3  # страница операций + договор + платежи


def test_documents_can_be_skipped_for_demo(engine: Engine, tmp_path: Path) -> None:
    api = _FakeAPI([])

    report = _run(api, engine, tmp_path, NOW, with_documents=False)

    assert "documents" not in api.paths
    assert report.documents == 0


def test_card_status_changes_are_kept(engine: Engine, tmp_path: Path) -> None:
    api = _FakeAPI([])
    _run(api, engine, tmp_path, NOW)
    _run(api, engine, tmp_path, datetime(2026, 10, 3, 9, 0))
    api.card_status = "Blocked"
    _run(api, engine, tmp_path, datetime(2026, 10, 4, 9, 0))

    with engine.connect() as conn:
        history = storage.load_status_history(conn, CONTRACT_KEY, "syn-c-001")

    assert [(item.previous_status, item.status) for item in history] == [
        (None, "Active"),
        ("Active", "Blocked"),
    ]
    assert history[1].seen_at == datetime(2026, 10, 4, 9, 0)
