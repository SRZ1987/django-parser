import re
from dataclasses import dataclass, field

from .normalization import normalize_brand, normalize_model, normalize_product_name, tokenize


KNOWN_BRANDS = {
    "bosch",
    "makita",
    "dewalt",
    "metabo",
    "milwaukee",
    "ryobi",
    "stanley",
    "fiskars",
    "karcher",
    "gardena",
    "knauf",
    "soudal",
    "tytan",
    "kiilto",
    "eskaro",
    "ceresit",
    "caparol",
}
MODEL_RE = re.compile(r"\b(?=[a-z0-9]*[a-z])(?=[a-z0-9]*\d)[a-z]{2,8}\d{2,6}[a-z0-9]*\b")
DIMENSION_RE = re.compile(r"\b(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(?:x(\d+(?:\.\d+)?))?(mm|cm|m)?\b")
NUMBER_UNIT_RE = re.compile(r"\b(\d+(?:\.\d+)?)(mm|cm|m|kg|g|l|ml|v|w|ah)\b")
PACK_RE = re.compile(r"\b(\d+)\s*(tk|pcs|pc|шт|шт.)\b")
BATTERY_RE = re.compile(r"\b(\d+)x(\d+(?:\.\d+)?)ah\b")
BUNDLE_TOKENS = {
    "set",
    "kit",
    "komplekt",
    "комплект",
    "набор",
    "case",
    "kohver",
    "akud",
    "aku",
    "зарядное",
    "аккумулятор",
    "аккумуляторы",
}


@dataclass(frozen=True)
class ProductAttributes:
    normalized_name: str
    tokens: set[str] = field(default_factory=set)
    brand: str = ""
    model: str = ""
    base_model: str = ""
    dimensions: tuple[str, ...] = ()
    diameter: str = ""
    length: str = ""
    width: str = ""
    height: str = ""
    weight: str = ""
    volume: str = ""
    quantity: int | None = None
    voltage: str = ""
    power: str = ""
    battery_count: int | None = None
    battery_capacity: str = ""
    is_bundle: bool = False


def extract_product_attributes(name: str, *, brand: str = "", model: str = "") -> ProductAttributes:
    normalized_name = normalize_product_name(name)
    tokens = set(tokenize(normalized_name))
    normalized_brand = _extract_brand(tokens, brand)
    normalized_model = _extract_model(normalized_name, model)
    dimensions = _extract_dimensions(normalized_name)
    measures = _extract_measures(normalized_name)
    battery = BATTERY_RE.search(normalized_name)

    return ProductAttributes(
        normalized_name=normalized_name,
        tokens=tokens,
        brand=normalized_brand,
        model=normalized_model,
        base_model=_base_model(normalized_model),
        dimensions=dimensions,
        diameter=dimensions[0] if len(dimensions) >= 1 else measures.get("diameter", ""),
        length=dimensions[1] if len(dimensions) >= 2 else measures.get("length", ""),
        width=dimensions[0] if len(dimensions) >= 2 else "",
        height=dimensions[2] if len(dimensions) >= 3 else "",
        weight=measures.get("weight", ""),
        volume=measures.get("volume", ""),
        quantity=_extract_quantity(normalized_name),
        voltage=measures.get("voltage", ""),
        power=measures.get("power", ""),
        battery_count=int(battery.group(1)) if battery else None,
        battery_capacity=f"{battery.group(2)}ah" if battery else "",
        is_bundle=_is_bundle(tokens, normalized_name),
    )


def _extract_brand(tokens: set[str], brand: str) -> str:
    normalized_brand = normalize_brand(brand)
    if normalized_brand:
        return normalized_brand
    for token in tokens:
        if token in KNOWN_BRANDS:
            return token
    return ""


def _extract_model(normalized_name: str, model: str) -> str:
    normalized_model = normalize_model(model)
    if normalized_model:
        return normalized_model
    match = MODEL_RE.search(normalized_name)
    return match.group(0) if match else ""


def _base_model(model: str) -> str:
    if not model:
        return ""
    match = re.match(r"^([a-z]{2,8}\d{2,6})[a-z0-9]*$", model)
    return match.group(1) if match else model


def _extract_dimensions(normalized_name: str) -> tuple[str, ...]:
    matches = list(DIMENSION_RE.finditer(normalized_name))
    if not matches:
        return ()
    matches.sort(key=lambda item: bool(item.group(4)), reverse=True)
    match = matches[0]
    if not match:
        return ()
    unit = match.group(4) or "mm"
    return tuple(f"{value}{unit}" for value in match.groups()[:3] if value)


def _extract_measures(normalized_name: str) -> dict[str, str]:
    measures = {}
    for value, unit in NUMBER_UNIT_RE.findall(normalized_name):
        normalized = f"{value}{unit}"
        if unit in {"kg", "g"}:
            measures.setdefault("weight", normalized)
        elif unit in {"l", "ml"}:
            measures.setdefault("volume", normalized)
        elif unit == "v":
            measures.setdefault("voltage", normalized)
        elif unit == "w":
            measures.setdefault("power", normalized)
        elif unit in {"mm", "cm", "m"}:
            measures.setdefault("length", normalized)
    return measures


def _extract_quantity(normalized_name: str) -> int | None:
    match = PACK_RE.search(normalized_name)
    return int(match.group(1)) if match else None


def _is_bundle(tokens: set[str], normalized_name: str) -> bool:
    if tokens & BUNDLE_TOKENS:
        return True
    if "+" in normalized_name:
        return True
    return bool(BATTERY_RE.search(normalized_name))
