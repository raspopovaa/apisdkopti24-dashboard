"""Локальный запуск прототипа дашборда: http://127.0.0.1:8765/.

Отдаёт prototype/dashboard.html, границы регионов, профиль синтетического договора и — если есть —
реальные транзакции из data/real/transactions.json (их готовит scripts/import_transactions_csv.py).
Файл с данными клиента не копируется в prototype/ и не попадает в git; сервер слушает только
loopback.

Запуск:

    python scripts/serve_prototype.py [порт]
"""

from __future__ import annotations

import errno
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE_HEAD = (
    b'<!doctype html>\n<meta charset="utf-8">\n'
    b'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
)
ROUTES: dict[str, tuple[Path, str]] = {
    "/russia-regions.json": (ROOT / "prototype" / "russia-regions.json", "application/json"),
    "/synthetic-profile.json": (ROOT / "prototype" / "synthetic-profile.json", "application/json"),
    "/real-transactions.json": (ROOT / "data" / "real" / "transactions.json", "application/json"),
}


class PrototypeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = PAGE_HEAD + (ROOT / "prototype" / "dashboard.html").read_bytes()
            self.send_body(body, "text/html; charset=utf-8")
            return
        route = ROUTES.get(path)
        if route is None or not route[0].is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_body(route[0].read_bytes(), f"{route[1]}; charset=utf-8")

    def send_body(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    real = ROUTES["/real-transactions.json"][0].is_file()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), PrototypeHandler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        sys.exit(
            f"Порт {port} занят — скорее всего, дашборд уже запущен: откройте http://127.0.0.1:{port}/.\n"
            f"Другой порт: python scripts/serve_prototype.py {port + 1}"
        )
    status = "есть" if real else "нет"
    print(f"Дашборд: http://127.0.0.1:{port}/ · реальные данные: {status} · остановка: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен.")


if __name__ == "__main__":
    main()
