"""Подготовка карты регионов России для дашборда (только стандартная библиотека).

Источник границ — Natural Earth 1:10m, Admin 1 (общественное достояние):
https://github.com/nvkelso/natural-earth-vector/blob/master/geojson/ne_10m_admin_1_states_provinces.geojson
(файл кладётся в data/raw/, папка исключена из git).

Регионы России отбираются по ``adm0_a3 == "RUS"`` — в этом наборе это вариант
«с точки зрения России». Подготовка следует методике статьи
https://habr.com/ru/articles/752406/: Чукотка не режется 180-м меридианом,
границы упрощаются, мелкие острова удаляются, крупные регионы рисуются первыми.
Вместо EPSG:32646 из статьи используется коническая равноугольная проекция
Ламберта: она хорошо передаёт форму России и считается без библиотеки PROJ.

Названия сопоставляются со справочником ``Region`` API (get_dictionary(name="Region")).
Регионы, которых в справочнике нет как отдельных (Ненецкий АО), присоединяются к
региону API, под которым проходят их АЗС.

Запуск:

    python scripts/build_regions_geo.py NE.geojson DICTIONARIES.json OUT.json
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

EARTH_RADIUS_KM = 6371.0
CENTRAL_MERIDIAN = 100.0
STANDARD_PARALLELS = (50.0, 70.0)
ORIGIN_LATITUDE = 60.0
# Допуск упрощения зависит от размера региона: мелкие (Москва, Севастополь) сохраняют форму.
SIMPLIFY_KM_BY_AREA = ((5_000.0, 0.15), (50_000.0, 0.6), (float("inf"), 1.5))
MIN_PART_AREA_KM2 = 300.0
MIN_PART_SHARE = 0.02  # у мелкого региона часть, крупная относительно него, не отбрасывается

# Natural Earth и справочник API называют часть регионов по-разному.
NAME_OVERRIDES = {
    "Автономная Республика Крым": "Республика Крым",
    "Ханты-Мансийский автономный округ - Югра": "ХМАО",
    "Ханты-Мансийский автономный округ — Югра": "ХМАО",
    "Ямало-Ненецкий автономный округ": "ЯНАО",
    "Ненецкий автономный округ": "Архангельская область",
    "Карачаево-Черкесия": "Карачаево-Черкесская республика",
    "Кабардино-Балкария": "Кабардино-Балкарская республика",
    "Удмуртия": "Удмуртская республика",
    "Чувашия": "Чувашская республика",
    "Чечня": "Чеченская республика",
    "Якутия": "Республика Саха (Якутия)",
}
# В Natural Earth у Алтайского края (name = "Altay") ошибочно указано name_ru «Республика Алтай».
NAME_OVERRIDES_EN = {
    "Altay": "Алтайский край",
    "Gorno-Altay": "Республика Алтай",
}


def normalize(name: str) -> str:
    """Ключ сравнения названий: без регистра, тире, ё, скобок и лишних пробелов."""
    text = name.lower().replace("ё", "е")
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[—–-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def api_region_names(dictionaries_path: Path) -> list[str]:
    payload = json.loads(dictionaries_path.read_text(encoding="utf-8"))
    rows = payload.get("dict_Region", []) if isinstance(payload, dict) else payload
    return [row["name"] for row in rows if row.get("country_id") == "RUS"]


def match_name(ne_name: str, api_names: list[str]) -> str | None:
    wanted = normalize(NAME_OVERRIDES.get(ne_name, ne_name))
    by_key = {normalize(name): name for name in api_names}
    if wanted in by_key:
        return by_key[wanted]
    candidates = [name for key, name in by_key.items() if wanted in key or key in wanted]
    return candidates[0] if len(candidates) == 1 else None


class LambertConformalConic:
    """Сферическая коническая равноугольная проекция Ламберта, координаты в километрах."""

    def __init__(self) -> None:
        phi1, phi2 = (math.radians(p) for p in STANDARD_PARALLELS)

        def t(phi: float) -> float:
            return math.tan(math.pi / 4 + phi / 2)

        self.n = math.log(math.cos(phi1) / math.cos(phi2)) / math.log(t(phi2) / t(phi1))
        self.f = math.cos(phi1) * t(phi1) ** self.n / self.n
        self.rho0 = EARTH_RADIUS_KM * self.f / t(math.radians(ORIGIN_LATITUDE)) ** self.n

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        # Долготы западнее 0° переносятся на восток: обе части Чукотки оказываются рядом.
        lon = lon + 360.0 if lon < 0 else lon
        rho = EARTH_RADIUS_KM * self.f / math.tan(math.pi / 4 + math.radians(lat) / 2) ** self.n
        theta = self.n * math.radians(lon - CENTRAL_MERIDIAN)
        return rho * math.sin(theta), self.rho0 - rho * math.cos(theta)


def douglas_peucker(
    points: list[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    if len(points) < 3:
        return points
    if points[0] == points[-1] and len(points) > 4:
        # У замкнутого контура совпадают концы: делим пополам, иначе отрезок вырождается в точку.
        middle = len(points) // 2
        return douglas_peucker(points[: middle + 1], tolerance)[:-1] + douglas_peucker(
            points[middle:], tolerance
        )
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        (x1, y1), (x2, y2) = points[start], points[end]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy) or 1e-12
        worst, index = 0.0, -1
        for i in range(start + 1, end):
            x0, y0 = points[i]
            distance = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / length
            if distance > worst:
                worst, index = distance, i
        if worst > tolerance and index > 0:
            keep[index] = True
            stack.extend([(start, index), (index, end)])
    return [point for point, kept in zip(points, keep, strict=True) if kept]


def ring_area(ring: list[tuple[float, float]]) -> float:
    edges = zip(ring, ring[1:] + ring[:1], strict=True)
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in edges)) / 2


def polygons_of(geometry: dict[str, Any]) -> list[list[list[list[float]]]]:
    coordinates: Any = geometry["coordinates"]
    if geometry["type"] == "Polygon":
        return [coordinates]
    if geometry["type"] == "MultiPolygon":
        multipolygon: list[list[list[list[float]]]] = coordinates
        return multipolygon
    return []


def tolerance_for(area_km2: float) -> float:
    return next(tol for limit, tol in SIMPLIFY_KM_BY_AREA if area_km2 < limit)


def prepare(
    geometry: dict[str, Any], projection: LambertConformalConic
) -> list[list[list[list[float]]]]:
    """Спроецировать, упростить и убрать мелкие острова; самая крупная часть остаётся всегда."""
    projected_polygons = [
        [[projection.project(lon, lat) for lon, lat in ring] for ring in polygon]
        for polygon in polygons_of(geometry)
    ]
    total_area = sum(ring_area(polygon[0]) for polygon in projected_polygons if polygon)
    tolerance = tolerance_for(total_area)
    parts: list[tuple[float, list[list[list[float]]]]] = []
    for polygon in projected_polygons:
        rings = []
        for projected in polygon:
            simplified = douglas_peucker(projected, tolerance)
            if len(simplified) >= 4:
                rings.append(simplified)
        if rings:
            parts.append(
                (ring_area(rings[0]), [[[round(x, 1), round(y, 1)] for x, y in r] for r in rings])
            )
    if not parts:
        return []
    parts.sort(key=lambda part: part[0], reverse=True)
    threshold = min(MIN_PART_AREA_KM2, total_area * MIN_PART_SHARE)
    return [coords for area, coords in parts if area >= threshold] or [parts[0][1]]


def build(ne_path: Path, dictionaries_path: Path, out_path: Path) -> None:
    source = json.loads(ne_path.read_text(encoding="utf-8"))
    projection = LambertConformalConic()
    api_names = api_region_names(dictionaries_path)
    merged: dict[str, dict[str, Any]] = {}
    unmatched: list[str] = []
    for feature in source["features"]:
        props = feature["properties"]
        if props.get("adm0_a3") != "RUS":
            continue
        ne_name = props.get("name_ru") or props.get("name")
        if not ne_name:
            continue  # безымянные мелкие острова (например, RU-X01~ у Ямала) не нужны
        api_name = NAME_OVERRIDES_EN.get(props.get("name")) or match_name(ne_name, api_names)
        if api_name is None:
            unmatched.append(ne_name)
            api_name = ne_name
        polygons = prepare(feature["geometry"], projection)
        entry = merged.setdefault(api_name, {"polygons": [], "iso": [], "area": 0.0})
        entry["polygons"].extend(polygons)
        entry["iso"].append(props.get("iso_3166_2"))
        entry["area"] += sum(ring_area([(p[0], p[1]) for p in poly[0]]) for poly in polygons)

    # Крупные регионы рисуются первыми, мелкие — поверх (как в статье).
    features = [
        {
            "type": "Feature",
            "properties": {"name": name, "iso": ",".join(filter(None, entry["iso"]))},
            "geometry": {"type": "MultiPolygon", "coordinates": entry["polygons"]},
        }
        for name, entry in sorted(merged.items(), key=lambda item: item[1]["area"], reverse=True)
    ]
    collection = {
        "type": "FeatureCollection",
        "projection": f"Lambert conformal conic, sphere R={EARTH_RADIUS_KM} km, "
        f"lon0={CENTRAL_MERIDIAN}, lat1/lat2={STANDARD_PARALLELS}, units km",
        # Параметры для проекции точек (АЗС) в браузере теми же формулами.
        "projectionParams": {
            "radiusKm": EARTH_RADIUS_KM,
            "lon0": CENTRAL_MERIDIAN,
            "lat1": STANDARD_PARALLELS[0],
            "lat2": STANDARD_PARALLELS[1],
            "latOrigin": ORIGIN_LATITUDE,
        },
        "source": "Natural Earth 1:10m Admin 1 (public domain), adm0_a3 = RUS",
        "features": features,
    }
    out_path.write_text(
        json.dumps(collection, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(f"регионов: {len(features)}; размер: {out_path.stat().st_size // 1024} КБ")
    print(f"не сопоставлены с API ({len(unmatched)}): {unmatched}")
    print(f"есть в API, нет на карте: {sorted(set(api_names) - set(merged))}")


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
