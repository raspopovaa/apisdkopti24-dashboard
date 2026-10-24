"""Загрузить договор из API ОПТИ 24 в базу и сверить итоги.

    uv run python -m fuel_portal.run_sync --env-file ~/.opti24-production.env \\
        --contract-id <ID договора> --with-money

Учётные данные берутся из окружения или файла --env-file: API_BASE_URL, API_KEY,
API_LOGIN, API_PASSWORD. ID договора можно задать через OPTI24_CONTRACT_ID или
AGENT_CONTRACT_ID. В вывод не попадают номера карт и ID договора.
"""

import argparse
import asyncio
import hashlib
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

from apisdkopti24 import APIClient, AsyncTransport, ConnectionSettings
from apisdkopti24.credentials import EnvironmentCredentialsProvider

from . import storage
from .reconcile import Check, check_contract, check_documents
from .sync import SyncReport, sync_contract

__all__ = ["contract_key_for", "main"]

_DEFAULT_DB = Path("data/portal.db")


def contract_key_for(contract_id: str) -> str:
    """Непрозрачный ключ договора для строк базы: ID договора в базу не пишется."""
    return "c-" + hashlib.sha256(contract_id.encode()).hexdigest()[:16]


def main(argv: Sequence[str] | None = None, transport: AsyncTransport | None = None) -> int:
    """transport — подставной транспорт для тестов; по умолчанию настоящий."""
    args = _parser().parse_args(argv)
    env_file = Path(args.env_file).expanduser() if args.env_file else Path(".env")
    settings = ConnectionSettings.from_env(env_file=env_file)
    credentials = EnvironmentCredentialsProvider.from_env(env_file=env_file)
    contract_id = (
        args.contract_id or os.getenv("OPTI24_CONTRACT_ID") or os.getenv("AGENT_CONTRACT_ID") or ""
    )
    if not settings.base_url or not contract_id:
        print("Нужны API_BASE_URL и ID договора (--contract-id).", file=sys.stderr)
        return 2

    db = Path(args.db).expanduser()
    db.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db}"
    storage.migrate(url)
    engine = storage.connect(url)
    key = contract_key_for(contract_id)
    now = datetime.now().replace(microsecond=0)

    async def run() -> SyncReport:
        async with APIClient(
            settings=settings, credentials_provider=credentials, transport=transport
        ) as client:
            await client.auth.auth_user(
                contract_id=contract_id
            )  # у учётной записи может быть несколько договоров
            return await sync_contract(
                client,
                engine,
                contract_key=key,
                contract_id=contract_id,
                now=now,
                first_day=date.fromisoformat(args.first_day) if args.first_day else None,
                max_pages=args.max_pages,
                with_documents=not args.no_documents,
                with_money=args.with_money,
            )

    report = asyncio.run(run())
    _print_report(report, db)

    with engine.connect() as conn:
        checks: list[Check] = []
        loaded = storage.load_contract_snapshot(conn, key)
        if loaded is not None:
            checks += check_contract(conn, key, *loaded)
        checks += check_documents(conn, key, report.date_from.replace(day=1), report.date_to)
    _print_checks(checks)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m fuel_portal.run_sync", description=__doc__)
    parser.add_argument("--env-file", help="файл с API_BASE_URL, API_KEY, API_LOGIN, API_PASSWORD")
    parser.add_argument("--contract-id", help="ID договора из API (не номер)")
    parser.add_argument(
        "--db", default=str(_DEFAULT_DB), help=f"база SQLite, по умолчанию {_DEFAULT_DB}"
    )
    parser.add_argument(
        "--first-day", help="первый день загрузки, ГГГГ-ММ-ДД; по умолчанию 1-е число"
    )
    parser.add_argument(
        "--max-pages", type=int, default=20, help="страниц операций на месяц, предел"
    )
    parser.add_argument(
        "--with-money", action="store_true", help="баланс и платежи: +2 тарифицируемых запроса"
    )
    parser.add_argument("--no-documents", action="store_true", help="без УПД (на DEMO их нет)")
    return parser


def _print_report(report: SyncReport, db: Path) -> None:
    print(f"Период: {report.date_from:%d.%m.%Y} – {report.date_to:%d.%m.%Y}")
    print(
        f"Загружено: карт {report.cards}, групп {report.card_groups}, лимитов {report.limits}, "
        f"операций {report.transactions}, документов {report.documents}, "
        f"платежей {report.payments}"
    )
    print(f"Снимок договора: {'да' if report.contract_snapshot else 'нет (нужен --with-money)'}")
    print(f"Тарифицируемых запросов: {report.billable_calls} (оценка)")
    print(f"База: {db}")


def _print_checks(checks: Sequence[Check]) -> None:
    if not checks:
        print("\nСверка: нечего сверять (нет снимка договора и документов).")
        return

    print("\nСверка (API − база):")
    for check in checks:
        mark = "совпадает" if check.matches else f"разница {check.difference}"
        print(f"- {check.name}: API {check.api_value}, база {check.db_value} — {mark}")
        print(f"  {check.note}")


if __name__ == "__main__":
    raise SystemExit(main())
