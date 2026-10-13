"""Профиль синтетического договора по транзакционному отчёту ЛК (CSV «1.9 Транзакционный»).

Скрипт сохраняет только обезличенные агрегаты, по которым дашборд генерирует правдоподобные
транзакции: доли товаров, распределения литров в чеке, медианные цены стелы по месяцам,
доли литров по типам АЗС, веса часов и дней недели, доли субъектов РФ, форму активности карт
и ставку сервисного сбора. Скидки синтетического договора задаются условиями в дашборде.
Номера карт, водители, ТС, комментарии, группы, адреса и номера АЗС, идентификаторы транзакций
и реквизиты договора в профиль не попадают.

АЗС синтетического договора выбираются из справочника API (get_azs_list_v1) в тех же
субъектах, а не из отчёта.

Запуск:

    python scripts/build_synthetic_profile.py ОТЧЁТ.csv prototype/russia-regions.json \\
        ДИКТ.json prototype/synthetic-profile.json
"""

from __future__ import annotations

import csv
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from import_transactions_csv import TZ_BY_REGION, RegionLocator, number, parse_coords

QUANTILE_POINTS = 11  # 0, 10, …, 100 %; крайние точки — 1-й и 99-й процентили
MIN_REGION_SHARE = 0.003
STATIONS_TOTAL = 170
OWN_BY_DICTIONARY = {
    "Own GPN": "Газпромнефть",
    "Own GPN-CS": "Газпромнефть",
    "RENT": "Газпромнефть",
    "EXT": "Партнёры",
    "FRAN": "Франчайзинг Газпромнефть",
    "OPTI": "ОПТИ 24",
}
OWN_BY_REPORT = {"Партнер": "Партнёры"}
SEED = 20261013


def quantiles(values: list[float], digits: int = 3) -> list[float]:
    ordered = sorted(values)
    if not ordered:
        return []
    points = []
    for i in range(QUANTILE_POINTS):
        share = min(0.99, max(0.01, i / (QUANTILE_POINTS - 1)))
        points.append(round(ordered[min(len(ordered) - 1, int(share * len(ordered)))], digits))
    return points


def shares(counter: Counter[str], digits: int = 4) -> dict[str, float]:
    total = sum(counter.values()) or 1
    return {key: round(value / total, digits) for key, value in counter.most_common()}


def goods_names(dictionaries: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"].upper(): row for row in dictionaries.get("dict_Goods", [])}


def read_report(csv_path: Path, locator: RegionLocator) -> list[dict[str, Any]]:
    located: dict[str, str | None] = {}
    rows = []
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            coords_text = raw["Координаты"].strip()
            if coords_text not in located:
                coords = parse_coords(coords_text)
                located[coords_text] = locator.locate(*coords) if coords else None
            rows.append(
                {
                    # Номер карты нужен только для формы активности и не покидает функцию build.
                    "card": raw["Номер карты"].strip(),
                    "time": datetime.strptime(raw["Время транзакции"].strip(), "%Y-%m-%d %H:%M:%S"),
                    "type": raw["Тип транзакции"].strip(),
                    "own": OWN_BY_REPORT.get(
                        raw["Принадлежность АЗС"].strip(), raw["Принадлежность АЗС"].strip()
                    ),
                    "goods": raw["Товар"].strip().replace("Аи-", "АИ-"),
                    "fuel": raw["Единица измерения"].strip() == "Литры",
                    "qty": number(raw["Количество"]),
                    "pump": number(raw["Цена стелы"]),
                    "sum_no_discount": number(raw["Сумма без скидки"]),
                    "discount": number(raw["Сумма скидки"]),
                    "fee": -number(raw["Сумма сервисного сбора"]),
                    "region": located[coords_text],
                    "grouped": bool(raw["Группа карт"].strip()),
                    "station": raw["АЗС"].strip(),
                }
            )
    return rows


def tx_per_day_by_month(
    purchases: list[dict[str, Any]], days: set[Any], month_offset: Callable[[datetime], int]
) -> dict[str, float]:
    """Заправок в день по месяцам: у клиента интенсивность заметно меняется от месяца к месяцу."""
    tx = Counter(month_offset(r["time"]) for r in purchases)
    day_count = Counter(month_offset(datetime.combine(d, datetime.min.time())) for d in days)
    return {str(k): round(tx[k] / day_count[k], 1) for k in sorted(tx)}


def card_shape(purchases: list[dict[str, Any]]) -> dict[str, Any]:
    by_card: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in purchases:
        by_card[row["card"]].append(row)
    home, main_fuel, stations = [], [], []
    for rows in by_card.values():
        regions = Counter(r["region"] for r in rows)
        home.append(regions.most_common(1)[0][1] / len(rows))
        litres = Counter[str]()
        for r in rows:
            if r["fuel"]:
                litres[r["goods"]] += r["qty"]
        if litres:
            main_fuel.append(litres.most_common(1)[0][1] / sum(litres.values()))
        stations.append(len({r["station"] for r in rows}))
    grouped = {row["card"] for row in purchases if row["grouped"]}
    return {
        "count": len(by_card),
        "txPerCard": quantiles([len(rows) for rows in by_card.values()], 1),
        "homeRegionShare": round(statistics.median(home), 3),
        "mainFuelShare": round(statistics.median(main_fuel), 3),
        "stationsPerCard": round(statistics.median(stations), 1),
        "groupedShare": round(len(grouped) / len(by_card), 3),
    }


def pick_stations(
    dictionaries: dict[str, Any],
    locator: RegionLocator,
    region_weights: dict[str, float],
    goods: dict[str, dict[str, Any]],
    pump_by_goods: dict[str, float],
) -> list[dict[str, Any]]:
    by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for azs in dictionaries.get("azs_v1", []):
        if azs.get("countryCode") != "RUS" or not azs.get("latitude"):
            continue
        try:
            lat, lon = float(azs["latitude"]), float(azs["longitude"])
        except ValueError:
            continue  # в справочнике встречаются битые координаты вида ".34.21"
        region = locator.locate(lat, lon) if 35 <= lat <= 82 else None
        own = OWN_BY_DICTIONARY.get(azs.get("ownType", ""))
        if region and own and azs.get("contractName"):
            by_region[region].append({"azs": azs, "lat": lat, "lon": lon, "own": own})
    code_to_name = {row["id"]: row["name"] for row in goods.values()}
    rnd = random.Random(SEED)
    picked = []
    for region, weight in region_weights.items():
        candidates = sorted(by_region.get(region, []), key=lambda c: c["azs"]["id"])
        rnd.shuffle(candidates)
        for cand in candidates[: max(2, min(25, round(weight * STATIONS_TOTAL)))]:
            azs = cand["azs"]
            prices = []
            for price in azs.get("prices") or []:
                name = code_to_name.get(price.get("GoodsCode"))
                value = float(price.get("Price") or 0)
                reference = pump_by_goods.get(name or "")
                # Цены справочника берём, только если они правдоподобны относительно отчёта.
                if name and reference and abs(value / reference - 1) <= 0.25:
                    prices.append(
                        {"name": name, "price": value, "dateFrom": price["DateFrom"][:10]}
                    )
            picked.append(
                {
                    "number": f"АЗС {azs['contractName'].strip()}",
                    "own": cand["own"],
                    "address": azs.get("full_address", "").strip(),
                    "region": region,
                    "lat": round(cand["lat"], 5),
                    "lon": round(cand["lon"], 5),
                    "tz": TZ_BY_REGION.get(region, 3),
                    "prices": prices,
                }
            )
    return picked


def build(csv_path: Path, regions_path: Path, dictionaries_path: Path, out_path: Path) -> None:
    locator = RegionLocator(json.loads(regions_path.read_text(encoding="utf-8")))
    dictionaries = json.loads(dictionaries_path.read_text(encoding="utf-8"))
    goods_dict = goods_names(dictionaries)
    rows = read_report(csv_path, locator)
    purchases = [r for r in rows if r["type"] == "Покупка"]
    fuel = [r for r in purchases if r["fuel"] and r["qty"] > 0]
    days = {r["time"].date() for r in rows}
    latest: datetime = max(r["time"] for r in rows)

    def month_offset(moment: datetime) -> int:
        return (latest.year - moment.year) * 12 + latest.month - moment.month

    weekday_days = Counter(d.weekday() for d in days)
    weekday_tx = Counter(r["time"].weekday() for r in purchases)
    weekday = [weekday_tx[i] / max(1, weekday_days[i]) for i in range(7)]
    hours = Counter(r["time"].hour for r in purchases)

    goods_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in purchases:
        goods_rows[r["goods"]].append(r)
    total_fuel_l = sum(r["qty"] for r in fuel)
    goods = []
    pump_by_goods: dict[str, float] = {}
    for name, items in sorted(goods_rows.items(), key=lambda kv: -len(kv[1])):
        ref = goods_dict.get(name.upper(), {})
        is_fuel = items[0]["fuel"]
        positive = [r for r in items if r["qty"] > 0]
        by_month: dict[int, list[float]] = defaultdict(list)
        for r in positive:
            if r["pump"] > 0:
                by_month[month_offset(r["time"])].append(r["pump"])
        pump = {str(k): round(statistics.median(v), 2) for k, v in sorted(by_month.items())}
        pump_by_goods[name] = statistics.median([r["pump"] for r in positive if r["pump"] > 0])
        goods.append(
            {
                "name": name,
                "code": ref.get("id"),
                "color": ref.get("hex_color"),
                "fuel": is_fuel,
                "txShare": round(len(items) / len(purchases), 5),
                "litreShare": (
                    round(sum(r["qty"] for r in positive) / total_fuel_l, 5) if is_fuel else 0
                ),
                "qty": quantiles([r["qty"] for r in positive], 1),
                "pumpByMonthOffset": pump,
            }
        )

    region_l: Counter[str] = Counter()
    for r in fuel:
        region_l[r["region"] or "Не определено"] += r["qty"]
    region_l.pop("Не определено", None)
    region_share = {k: v for k, v in shares(region_l).items() if v >= MIN_REGION_SHARE}
    # Индекс цены субъекта: медиана отношения цены стелы к медиане товара за месяц.
    month_median: dict[tuple[str, int], float] = {}
    for g in goods:
        for k, v in g["pumpByMonthOffset"].items():
            month_median[(g["name"], int(k))] = v
    region_ratio: dict[str, list[float]] = defaultdict(list)
    for r in fuel:
        base = month_median.get((r["goods"], month_offset(r["time"])))
        if r["region"] in region_share and base and r["pump"] > 0:
            region_ratio[r["region"]].append(r["pump"] / base)

    # Скидки синтетического договора задаются условиями в дашборде; из отчёта нужны только доли
    # литров по типам АЗС — по ним выбираются станции.
    own_l: Counter[str] = Counter()
    for r in fuel:
        own_l[r["own"]] += r["qty"]
    own = {name: {"litreShare": share} for name, share in shares(own_l).items()}

    stations = pick_stations(dictionaries, locator, region_share, goods_dict, pump_by_goods)
    covered = {s["region"] for s in stations}
    region_weights = {k: v for k, v in region_share.items() if k in covered}
    weight_sum = sum(region_weights.values())
    payable = sum(r["sum_no_discount"] - r["discount"] for r in rows)
    profile: dict[str, Any] = {
        "meta": {
            "source": "агрегаты транзакционного отчёта ЛК; без транзакций, карт и реквизитов",
            "periodDays": len(days),
            "builtAt": datetime.now().strftime("%Y-%m-%d"),
            "regionsWithoutDictionaryStations": sorted(set(region_share) - covered),
        },
        "txPerDay": round(len(purchases) / len(days), 1),
        "txPerDayByMonthOffset": tx_per_day_by_month(purchases, days, month_offset),
        "returnShare": round((len(rows) - len(purchases)) / len(rows), 4),
        "feeRate": round(sum(r["fee"] for r in rows) / payable, 4) if payable else 0,
        "weekday": [round(w / max(weekday), 3) for w in weekday],
        "hour": [round(hours[h] / max(hours.values()), 3) for h in range(24)],
        "goods": goods,
        "own": own,
        "regions": {
            k: {
                "share": round(v / weight_sum, 4),
                "priceIndex": (
                    round(statistics.median(region_ratio[k]), 3) if region_ratio[k] else 1.0
                ),
            }
            for k, v in region_weights.items()
        },
        "cards": card_shape(purchases),
        "stations": stations,
    }
    out_path.write_text(
        json.dumps(profile, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(
        f"профиль: {len(goods)} товаров, {len(region_weights)} субъектов, "
        f"{len(stations)} АЗС из справочника; без АЗС в справочнике: "
        f"{', '.join(profile['meta']['regionsWithoutDictionaryStations']) or '—'}; "
        f"{out_path.stat().st_size // 1024} КБ"
    )


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
