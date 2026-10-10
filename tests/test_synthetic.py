import json
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal

from fuel_portal.domain import CardStatus, ProductCategory, mask_card_number
from fuel_portal.synthetic import AnomalyKind, generate, to_json

SYNTHETIC_PREFIX = "9999"
NIGHT_END_HOUR = 5
KOPECK = Decimal("0.01")


def test_same_seed_gives_same_data() -> None:
    assert to_json(generate(seed=7)) == to_json(generate(seed=7))
    assert to_json(generate(seed=7)) != to_json(generate(seed=8))


def test_identifiers_are_clearly_synthetic() -> None:
    dataset = generate()

    assert all(card.number.startswith(SYNTHETIC_PREFIX) for card in dataset.cards)
    assert all(card.id.startswith("syn-c-") for card in dataset.cards)
    assert all(station.id.startswith("syn-s-") for station in dataset.stations)
    assert all(item.id.startswith("syn-t-") for item in dataset.transactions)
    assert len({item.id for item in dataset.transactions}) == len(dataset.transactions)


def test_amounts_follow_price_and_discount() -> None:
    for item in generate().transactions:
        assert item.amount == (item.qty * item.price).quantize(KOPECK, ROUND_HALF_UP)
        assert item.price <= item.price_no_discount
        # У сторно скидка отрицательная, как и количество.
        assert item.discount * item.qty >= 0


def test_every_anomaly_kind_is_injected_and_labeled() -> None:
    dataset = generate()
    by_id = {item.id: item for item in dataset.transactions}

    assert Counter(dataset.anomalies.values()) == dict.fromkeys(AnomalyKind, 2)
    assert set(dataset.anomalies) <= set(by_id)
    for transaction_id, kind in dataset.anomalies.items():
        if kind is AnomalyKind.STORNO:
            assert by_id[transaction_id].is_storno
            assert by_id[transaction_id].qty < 0


def test_regular_refuels_happen_by_day_and_at_home() -> None:
    dataset = generate()
    home = {card.id: card.home_region for card in dataset.cards}
    region = {station.id: station.region for station in dataset.stations}

    for item in dataset.transactions:
        if item.id in dataset.anomalies:
            continue
        assert item.timestamp.hour >= NIGHT_END_HOUR
        assert region[item.station_id] == home[item.card_id]


def test_blocked_card_has_no_transactions() -> None:
    dataset = generate(card_count=5)
    blocked = [card for card in dataset.cards if card.status is CardStatus.BLOCKED]

    assert [card.id for card in blocked] == ["syn-c-005"]
    assert not [item for item in dataset.transactions if item.card_id == "syn-c-005"]


def test_goods_are_bought_alongside_fuel() -> None:
    categories = Counter(item.product.category for item in generate().transactions)

    assert categories[ProductCategory.FUEL] > categories[ProductCategory.GOODS] > 0


def test_json_export_round_trips() -> None:
    payload = json.loads(json.dumps(to_json(generate(card_count=3, days=10)), default=str))

    assert set(payload) == {"cards", "stations", "transactions", "limits", "anomalies"}
    assert payload["cards"][0]["number"].startswith(SYNTHETIC_PREFIX)


def test_card_number_is_masked() -> None:
    assert mask_card_number("9999000000000123") == "**** 0123"
