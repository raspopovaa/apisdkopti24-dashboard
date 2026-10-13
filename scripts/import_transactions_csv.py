"""Импорт транзакционного отчёта из личного кабинета (CSV «1.9 Транзакционный») для прототипа.

Результат — компактный JSON для локальной проверки дашборда на реальных данных. Файл с
данными клиента кладётся в data/real/ (папка исключена из git) и не публикуется.

Что делает скрипт:
- отбрасывает персональные и учётные данные: водителя, ТС, наименование клиента, ИНН,
  номер договора, RRN, ID терминала; номер карты сохраняет только последними 4 цифрами;
- определяет субъект РФ по координатам АЗС (полигоны prototype/russia-regions.json), потому
  что «Регион АЗС» в отчёте — подразделение ГПН, а не субъект; часовой пояс — по субъекту;
- переводит местное время АЗС в UTC.

Запуск:

    python scripts/import_transactions_csv.py ОТЧЁТ.csv prototype/russia-regions.json \\
        ДИКТ.json data/real/transactions.json
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Смещение от UTC в часах по субъектам; остальные регионы — UTC+3.
TZ_BY_REGION = {
    "Калининградская область": 2,
    "Самарская область": 4,
    "Удмуртская республика": 4,
    "Ульяновская область": 4,
    "Астраханская область": 4,
    "Саратовская область": 4,
    "Республика Башкортостан": 5,
    "Пермский край": 5,
    "Оренбургская область": 5,
    "Свердловская область": 5,
    "Челябинская область": 5,
    "Курганская область": 5,
    "Тюменская область": 5,
    "ХМАО": 5,
    "ЯНАО": 5,
    "Омская область": 6,
    "Новосибирская область": 7,
    "Томская область": 7,
    "Кемеровская область": 7,
    "Алтайский край": 7,
    "Республика Алтай": 7,
    "Красноярский край": 7,
    "Республика Хакасия": 7,
    "Республика Тыва": 7,
    "Иркутская область": 8,
    "Республика Бурятия": 8,
    "Забайкальский край": 9,
    "Амурская область": 9,
    "Республика Саха (Якутия)": 9,
    "Приморский край": 10,
    "Хабаровский край": 10,
    "Еврейская автономная область": 10,
    "Сахалинская область": 11,
    "Магаданская область": 11,
    "Камчатский край": 12,
    "Чукотский автономный округ": 12,
}
# Плотность для пересчёта литров в килограммы (кг/л).
DENSITY = {"diesel": 0.84, "gaz": 0.54, "fuel": 0.745}
OWN_NAMES = {"Партнер": "Партнёры"}


def number(value: str) -> float:
    value = value.strip().replace(",", ".")
    return float(value) if value else 0.0


def make_projection(params: dict[str, float]) -> Callable[[float, float], tuple[float, float]]:
    rad = math.pi / 180

    def tg(phi: float) -> float:
        return math.tan(math.pi / 4 + phi / 2)

    phi1, phi2 = params["lat1"] * rad, params["lat2"] * rad
    n = math.log(math.cos(phi1) / math.cos(phi2)) / math.log(tg(phi2) / tg(phi1))
    f = math.cos(phi1) * tg(phi1) ** n / n
    rho0 = params["radiusKm"] * f / tg(params["latOrigin"] * rad) ** n

    def project(lon: float, lat: float) -> tuple[float, float]:
        lon = lon + 360.0 if lon < 0 else lon
        rho = params["radiusKm"] * f / tg(lat * rad) ** n
        theta = n * (lon - params["lon0"]) * rad
        return rho * math.sin(theta), rho0 - rho * math.cos(theta)

    return project


class RegionLocator:
    """Субъект РФ по точке: внешние контуры регионов карты с отсечением по рамке."""

    def __init__(self, regions: dict[str, Any]) -> None:
        self.project = make_projection(regions["projectionParams"])
        self.rings: list[tuple[str, tuple[float, float, float, float], list[list[float]]]] = []
        for feature in regions["features"]:
            for polygon in feature["geometry"]["coordinates"]:
                ring = polygon[0]
                xs, ys = [p[0] for p in ring], [p[1] for p in ring]
                box = (min(xs), min(ys), max(xs), max(ys))
                self.rings.append((feature["properties"]["name"], box, ring))

    def locate(self, lat: float, lon: float) -> str | None:
        x, y = self.project(lon, lat)
        for name, (x0, y0, x1, y1), ring in self.rings:
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                continue
            inside = False
            edges = zip(ring, ring[1:] + ring[:1], strict=True)
            for (ax, ay), (bx, by) in edges:
                if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                    inside = not inside
            if inside:
                return name
        return None


def parse_coords(value: str) -> tuple[float, float] | None:
    parts = [p for p in value.replace(" ", "").split(",") if p]
    if len(parts) != 2:
        return None
    lat, lon = float(parts[0]), float(parts[1])
    return (lat, lon) if 35 <= lat <= 82 else None


def goods_catalog(dictionaries_path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(dictionaries_path.read_text(encoding="utf-8"))
    return {row["name"].upper(): row for row in payload.get("dict_Goods", [])}


def build(csv_path: Path, regions_path: Path, dictionaries_path: Path, out_path: Path) -> None:
    locator = RegionLocator(json.loads(regions_path.read_text(encoding="utf-8")))
    goods_dict = goods_catalog(dictionaries_path)
    stations: dict[tuple[str, str], int] = {}
    station_rows: list[dict[str, Any]] = []
    cards: dict[str, int] = {}
    card_rows: list[dict[str, Any]] = []
    goods: dict[str, int] = {}
    goods_rows: list[dict[str, Any]] = []
    tx: list[list[Any]] = []
    fee_total = sum_total = 0.0
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            card_number = row["Номер карты"].strip()
            if card_number not in cards:
                cards[card_number] = len(card_rows)
                card_rows.append(
                    {
                        "last4": card_number[-4:],
                        "comment": row["Комментарий клиента"].strip(),
                        "group": row["Группа карт"].strip() or None,
                    }
                )
            coords = parse_coords(row["Координаты"])
            key = (row["АЗС"].strip(), row["Координаты"].strip())
            if key not in stations:
                region = locator.locate(*coords) if coords else None
                stations[key] = len(station_rows)
                station_rows.append(
                    {
                        "number": f"АЗС {row['АЗС'].strip()}",
                        "own": OWN_NAMES.get(
                            row["Принадлежность АЗС"].strip(), row["Принадлежность АЗС"].strip()
                        ),
                        "owner": row["Владелец АЗС"].strip(),
                        "address": row["Адрес АЗС"].strip(),
                        "region": region or "Не определено",
                        "lat": coords[0] if coords else None,
                        "lon": coords[1] if coords else None,
                        "tz": TZ_BY_REGION.get(region or "", 3),
                    }
                )
            name = row["Товар"].strip()
            if name not in goods:
                ref = goods_dict.get(name.upper(), {})
                group = ref.get("group_code") or ""
                is_fuel = row["Единица измерения"].strip() == "Литры"
                goods[name] = len(goods_rows)
                goods_rows.append(
                    {
                        "name": name.replace("Аи-", "АИ-"),
                        "code": ref.get("id"),
                        "color": ref.get("hex_color"),
                        "fuel": is_fuel,
                        "density": (
                            DENSITY.get(
                                group,
                                DENSITY["diesel"] if "ДТ" in name.upper() else DENSITY["fuel"],
                            )
                            if is_fuel
                            else None
                        ),
                    }
                )
            station = station_rows[stations[key]]
            local = datetime.strptime(row["Время транзакции"].strip(), "%Y-%m-%d %H:%M:%S")
            utc = (local - timedelta(hours=station["tz"])).replace(tzinfo=UTC)
            sum_wo, disc = number(row["Сумма без скидки"]), number(row["Сумма скидки"])
            # В отчёте сбор с минусом (списание); у возвратов знак обратный — сбор возвращается.
            fee = -number(row["Сумма сервисного сбора"])
            fee_total += fee
            sum_total += sum_wo - disc
            tx.append(
                [
                    local.strftime("%Y-%m-%dT%H:%M:%S"),
                    int(utc.timestamp() * 1000),
                    cards[card_number],
                    stations[key],
                    goods[name],
                    number(row["Количество"]),
                    number(row["Цена стелы"]),
                    number(row["Цена клиента"]),
                    sum_wo,
                    disc,
                    fee,
                    row["Тип транзакции"].strip(),
                    row["ID транзакции"].strip(),
                ]
            )
    tx.sort(key=lambda item: item[1])
    payload: dict[str, Any] = {
        "meta": {
            "source": "Транзакционный отчёт ЛК (CSV 1.9), без персональных данных",
            "rows": len(tx),
            "from": tx[0][0][:10] if tx else None,
            "to": tx[-1][0][:10] if tx else None,
            "feeRate": round(fee_total / sum_total, 4) if sum_total else 0,
            "columns": [
                "local",
                "utc",
                "card",
                "station",
                "goods",
                "qty",
                "pricePump",
                "priceClient",
                "sumNoDiscount",
                "discount",
                "fee",
                "type",
                "id",
            ],
        },
        "goods": goods_rows,
        "stations": station_rows,
        "cards": card_rows,
        "tx": tx,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    located = sum(1 for s in station_rows if s["region"] != "Не определено")
    print(
        f"транзакций: {len(tx)}; карт: {len(card_rows)}; "
        f"АЗС: {len(station_rows)} (субъект найден: {located}); "
        f"товаров: {len(goods_rows)}; ставка сбора ≈ {payload['meta']['feeRate']:.2%}; "
        f"размер: {out_path.stat().st_size // 1024} КБ"
    )


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
