"""Детерминированные синтетические данные автопарка для демо и тестов.

Реальные данные топливных карт — персональные и коммерческие, поэтому публичная
демо-версия и тесты работают только на синтетике. Один и тот же seed всегда даёт
одни и те же данные. Генератор закладывает аномалии известных видов и возвращает
их список: по нему проверяются детекторы аномалий.

    python -m fuel_portal.synthetic --out data/synthetic.json
"""

import argparse
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from fuel_portal.domain import (
    Card,
    CardStatus,
    Limit,
    Product,
    ProductCategory,
    Station,
    Transaction,
)

__all__ = ["AnomalyKind", "SyntheticDataset", "generate", "main", "to_json"]

DEFAULT_SEED = 2026
DEFAULT_CARDS = 24
DEFAULT_DAYS = 90
DEFAULT_END = date(2026, 9, 30)

# Префикс 9999 не выдаётся настоящим картам: синтетика не совпадёт с реальной.
_CARD_PREFIX = "9999"
_CARD_DIGITS = 12

_REGIONS = {
    "Москва": (55.7558, 37.6176),
    "Новосибирск": (55.0302, 82.9204),
    "Екатеринбург": (56.8389, 60.6057),
    "Казань": (55.7963, 49.1088),
}
_BRANDS = ("Сеть А", "Сеть Б", "Сеть В")
_STATIONS_PER_REGION = 6
_STATION_SPREAD_DEGREES = 0.15

_GROUPS = ("Доставка", "Сервис", "Руководство")
_GROUP_FUEL = {"Доставка": Product.DIESEL, "Сервис": Product.AI92, "Руководство": Product.AI95}

_BASE_PRICE = {
    Product.AI92: Decimal("52.40"),
    Product.AI95: Decimal("57.90"),
    Product.DIESEL: Decimal("64.10"),
    Product.COFFEE: Decimal("150.00"),
    Product.WASHER: Decimal("390.00"),
}
_PRICE_NOISE = 0.02
_DISCOUNT_RATE = Decimal("0.03")
_KOPECK = Decimal("0.01")
_LITER_STEP = Decimal("0.01")

_REFUEL_EVERY_DAYS = (2, 4)
_REFUEL_LITERS = (25.0, 60.0)
_DAY_HOURS = (7, 21)
_GOODS_SHARE = 0.12
_LIMIT_LITERS = Decimal("600")

_ANOMALIES_PER_KIND = 2
_OVER_TANK_LITERS = Decimal("95.00")
_RAPID_GAP = timedelta(minutes=40)
_NIGHT_HOURS = (1, 4)


class AnomalyKind(StrEnum):
    OVER_TANK = "over_tank"  # объём больше бака
    RAPID_REFUEL = "rapid_refuel"  # повторная заправка вскоре после предыдущей
    NIGHT_REFUEL = "night_refuel"  # заправка ночью
    FAR_REGION = "far_region"  # АЗС вне обычного региона карты
    STORNO = "storno"  # отмена операции


@dataclass(frozen=True, slots=True)
class SyntheticDataset:
    cards: tuple[Card, ...]
    stations: tuple[Station, ...]
    transactions: tuple[Transaction, ...]
    limits: tuple[Limit, ...]
    anomalies: dict[str, AnomalyKind] = field(default_factory=dict)


def generate(
    *,
    seed: int = DEFAULT_SEED,
    card_count: int = DEFAULT_CARDS,
    days: int = DEFAULT_DAYS,
    end: date = DEFAULT_END,
) -> SyntheticDataset:
    """Сгенерировать автопарк: карты, АЗС, транзакции за ``days`` дней до ``end``."""
    rng = random.Random(seed)
    stations = _stations(rng)
    cards = tuple(_card(index, card_count) for index in range(1, card_count + 1))
    start = end - timedelta(days=days - 1)

    transactions: list[Transaction] = []
    for card in cards:
        transactions.extend(_card_history(rng, card, stations, start, end))

    anomalies = _inject(rng, cards, stations, transactions)
    transactions.sort(key=lambda item: (item.timestamp, item.id))
    limits = tuple(
        Limit(f"syn-l-{card.id}", _fuel(card), card_id=card.id, liters=_LIMIT_LITERS, unit="LIT")
        for card in cards
    )
    return SyntheticDataset(cards, stations, tuple(transactions), limits, anomalies)


def _stations(rng: random.Random) -> tuple[Station, ...]:
    stations = []
    for region_index, (region, (lat, lon)) in enumerate(_REGIONS.items(), start=1):
        for number in range(1, _STATIONS_PER_REGION + 1):
            stations.append(
                Station(
                    id=f"syn-s-{region_index:02d}-{number:02d}",
                    brand=_BRANDS[(number - 1) % len(_BRANDS)],
                    region=region,
                    latitude=round(lat + rng.uniform(-1, 1) * _STATION_SPREAD_DEGREES, 5),
                    longitude=round(lon + rng.uniform(-1, 1) * _STATION_SPREAD_DEGREES, 5),
                )
            )
    return tuple(stations)


def _card(index: int, card_count: int) -> Card:
    group = _GROUPS[(index - 1) % len(_GROUPS)]
    regions = tuple(_REGIONS)

    # Последняя карта заблокирована: на дашборде нужен пример неактивной карты.
    status = CardStatus.BLOCKED if index == card_count else CardStatus.ACTIVE
    return Card(
        id=f"syn-c-{index:03d}",
        number=f"{_CARD_PREFIX}{index:0{_CARD_DIGITS}d}",
        group=group,
        status=status,
        product=_GROUP_FUEL[group],
        home_region=regions[(index - 1) % len(regions)],
    )


def _card_history(
    rng: random.Random,
    card: Card,
    stations: tuple[Station, ...],
    start: date,
    end: date,
) -> list[Transaction]:
    if card.status is CardStatus.BLOCKED:
        return []

    home = [station for station in stations if station.region == card.home_region]
    history: list[Transaction] = []
    day = start + timedelta(days=rng.randint(0, _REFUEL_EVERY_DAYS[1]))
    while day <= end:
        moment = _daytime(rng, day)
        liters = Decimal(str(rng.uniform(*_REFUEL_LITERS))).quantize(_LITER_STEP)
        history.append(_purchase(rng, card, rng.choice(home), _fuel(card), liters, moment))

        if rng.random() < _GOODS_SHARE:
            goods = rng.choice((Product.COFFEE, Product.WASHER))
            later = moment + timedelta(minutes=rng.randint(1, 5))
            history.append(_purchase(rng, card, history[-1].station_id, goods, Decimal(1), later))

        day += timedelta(days=rng.randint(*_REFUEL_EVERY_DAYS))
    return history


def _fuel(card: Card) -> Product:
    # У синтетической карты топливо задано всегда.
    if card.product is None:
        raise ValueError(f"У карты {card.id} не задано топливо")

    return Product(card.product)


def _daytime(rng: random.Random, day: date) -> datetime:
    clock = time(rng.randint(*_DAY_HOURS), rng.randint(0, 59))
    return datetime.combine(day, clock)


def _purchase(
    rng: random.Random,
    card: Card,
    station: Station | str,
    product: Product,
    qty: Decimal,
    moment: datetime,
) -> Transaction:
    station_id = station if isinstance(station, str) else station.id
    noise = Decimal(str(1 + rng.uniform(-_PRICE_NOISE, _PRICE_NOISE)))
    full_price = (_BASE_PRICE[product] * noise).quantize(_KOPECK, ROUND_HALF_UP)
    price = (full_price * (1 - _DISCOUNT_RATE)).quantize(_KOPECK, ROUND_HALF_UP)
    amount = (qty * price).quantize(_KOPECK, ROUND_HALF_UP)
    full_amount = (qty * full_price).quantize(_KOPECK, ROUND_HALF_UP)
    return Transaction(
        id=f"syn-t-{rng.getrandbits(40):010x}",
        timestamp=moment,
        card_id=card.id,
        station_id=station_id,
        product=product,
        qty=qty,
        price=price,
        price_no_discount=full_price,
        amount=amount,
        discount=full_amount - amount,
        is_storno=False,
    )


def _inject(
    rng: random.Random,
    cards: tuple[Card, ...],
    stations: tuple[Station, ...],
    transactions: list[Transaction],
) -> dict[str, AnomalyKind]:
    """Заложить по несколько аномалий каждого вида; вернуть ID транзакций и вид."""
    fuel = [item for item in transactions if Product(item.product).category is ProductCategory.FUEL]
    by_card = {card.id: card for card in cards}
    anomalies: dict[str, AnomalyKind] = {}

    for kind in AnomalyKind:
        for base in rng.sample(fuel, _ANOMALIES_PER_KIND):
            card = by_card[base.card_id]
            anomaly = _anomaly(rng, kind, card, base, stations)
            transactions.append(anomaly)
            anomalies[anomaly.id] = kind
    return anomalies


def _anomaly(
    rng: random.Random,
    kind: AnomalyKind,
    card: Card,
    base: Transaction,
    stations: tuple[Station, ...],
) -> Transaction:
    day = base.timestamp.date()
    if kind is AnomalyKind.OVER_TANK:
        later = base.timestamp + timedelta(days=1)
        return _purchase(rng, card, base.station_id, _fuel(card), _OVER_TANK_LITERS, later)

    if kind is AnomalyKind.RAPID_REFUEL:
        later = base.timestamp + _RAPID_GAP
        return _purchase(rng, card, base.station_id, _fuel(card), base.qty, later)

    if kind is AnomalyKind.NIGHT_REFUEL:
        night = datetime.combine(day, time(rng.randint(*_NIGHT_HOURS), rng.randint(0, 59)))
        return _purchase(rng, card, base.station_id, _fuel(card), base.qty, night)

    if kind is AnomalyKind.FAR_REGION:
        far = [station for station in stations if station.region != card.home_region]
        later = base.timestamp + timedelta(hours=3)
        return _purchase(rng, card, rng.choice(far), _fuel(card), base.qty, later)

    # Сторно повторяет исходную операцию с обратным знаком.
    return Transaction(
        id=f"syn-t-{rng.getrandbits(40):010x}",
        timestamp=base.timestamp + timedelta(minutes=10),
        card_id=base.card_id,
        station_id=base.station_id,
        product=base.product,
        qty=-base.qty,
        price=base.price,
        price_no_discount=base.price_no_discount,
        amount=-base.amount,
        discount=-base.discount,
        is_storno=True,
    )


def to_json(dataset: SyntheticDataset) -> dict[str, Any]:
    """Представить набор в JSON: Decimal — строкой, время — ISO 8601."""
    return {
        "cards": [asdict(card) for card in dataset.cards],
        "stations": [asdict(station) for station in dataset.stations],
        "transactions": [asdict(item) for item in dataset.transactions],
        "limits": [asdict(limit) for limit in dataset.limits],
        "anomalies": {key: kind.value for key, kind in dataset.anomalies.items()},
    }


def _encode(value: object) -> str:
    if isinstance(value, Decimal | datetime):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    raise TypeError(f"Тип {type(value).__name__} не сериализуется в JSON")


def main() -> None:
    parser = argparse.ArgumentParser(description="Синтетические данные автопарка")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--cards", type=int, default=DEFAULT_CARDS)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    dataset = generate(seed=args.seed, card_count=args.cards, days=args.days)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_json(dataset), ensure_ascii=False, indent=1, default=_encode)
    args.out.write_text(text + "\n", encoding="utf-8")
    print(f"{len(dataset.transactions)} транзакций, {len(dataset.anomalies)} аномалий → {args.out}")


if __name__ == "__main__":
    main()
