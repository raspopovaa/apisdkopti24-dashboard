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


class _FakeAPI:
    """Подставной API: операции по дням; запоминает запрошенные периоды."""

    def __init__(self, operations: list[dict[str, Any]]) -> None:
        self.operations = operations
        self.periods: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v1/authUser"):
            return httpx.Response(200, json=_envelope(_AUTH), request=request)

        if path.endswith("/v2/cards"):
            data = {"total_count": 1, "result": [_CARD]}
            return httpx.Response(200, json=_envelope(data), request=request)

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
