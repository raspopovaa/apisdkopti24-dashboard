import os
from pathlib import Path
from typing import cast

import httpx
import pytest
from apisdkopti24 import AsyncTransport, RateLimitPolicy
from apisdkopti24.transport import AsyncHTTPClient
from test_sync import BASE_URL, CONTRACT_ID, _FakeAPI, _operation

from fuel_portal.run_sync import contract_key_for, main


def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Файл окружения пишет в os.environ: копия не даёт ему протечь в другие тесты.
    names = ("API_", "OPTI24_", "AGENT_")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(names)}
    monkeypatch.setattr(os, "environ", clean)


def _env(tmp_path: Path) -> Path:
    env = tmp_path / "opti24.env"
    env.write_text(
        f"API_BASE_URL={BASE_URL}\nAPI_KEY=key\nAPI_LOGIN=demo\nAPI_PASSWORD=password\n",
        encoding="utf-8",
    )
    return env


def test_run_prints_report_without_card_numbers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_environment(monkeypatch)
    api = _FakeAPI([_operation(1, "2026-10-01")])
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    transport = AsyncTransport(
        BASE_URL,
        http_client=cast(AsyncHTTPClient, http_client),
        rate_limit_policy=RateLimitPolicy(requests_per_second=10_000.0),
    )
    args = [
        "--env-file",
        str(_env(tmp_path)),
        "--contract-id",
        CONTRACT_ID,
        "--db",
        str(tmp_path / "portal.db"),
        "--first-day",
        "2026-10-01",
        "--with-money",
    ]

    code = main(args, transport=transport)

    out = capsys.readouterr().out
    assert code == 0
    assert "операций 1" in out
    assert "Расход месяца, ₽" in out
    assert "9999000000000001" not in out
    assert CONTRACT_ID not in out


def test_missing_contract_id_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolated_environment(monkeypatch)

    assert main(["--env-file", str(_env(tmp_path)), "--db", str(tmp_path / "p.db")]) == 2


def test_contract_key_hides_contract_id() -> None:
    key = contract_key_for(CONTRACT_ID)

    assert key == contract_key_for(CONTRACT_ID)
    assert CONTRACT_ID not in key
