import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .normalization import (
    is_number_token,
    normalize_brand,
    normalize_model,
    normalize_product_name,
    parse_dimension_token,
    parse_measure_token,
    tokenize,
)


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
    "jasper",
    "oregon",
    "suki",
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
NUMBER_UNIT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)(mm|cm|m|mg|kg|g|cl|ml|l|kw|w|v|mah|ah|a|bar|kpa|mpa|pa|nm|hz)\b"
)
PACK_RE = re.compile(r"\b(\d+)\s*(tk|pcs|pc|шт|шт.)\b")
BATTERY_RE = re.compile(r"\b(\d+)x(\d+(?:\.\d+)?)ah\b")
PRODUCT_TYPE_STOP_WORDS = {
    "aku",
    "akuga",
    "akutoitega",
    "bensiinimootoriga",
    "elektriline",
    "elektrimootoriga",
    "hele",
    "international",
    "juhtmeta",
    "kaabel",
    "kompaktne",
    "komplekt",
    "kollane",
    "must",
    "professionaalne",
    "professional",
    "puitm",
    "punane",
    "roheline",
    "sinine",
    "tarvik",
    "tarvikud",
    "toode",
    "tooted",
    "universaalne",
    "uus",
    "valge",
}
GENERIC_CATEGORY_TOKENS = {
    "aiakaubad",
    "ehitusmaterjalid",
    "kinnitusvahendid",
    "lisatarvikud",
    "materjalid",
    "seadmed",
    "tarvikud",
    "tooted",
    "tööriistad",
}
MEASURE_FACTORS = {
    "mm": ("length", Decimal("1")),
    "cm": ("length", Decimal("10")),
    "m": ("length", Decimal("1000")),
    "mg": ("weight", Decimal("0.001")),
    "g": ("weight", Decimal("1")),
    "kg": ("weight", Decimal("1000")),
    "ml": ("volume", Decimal("1")),
    "cl": ("volume", Decimal("10")),
    "l": ("volume", Decimal("1000")),
    "w": ("power", Decimal("1")),
    "kw": ("power", Decimal("1000")),
    "v": ("voltage", Decimal("1")),
    "a": ("current", Decimal("1")),
    "mah": ("battery_capacity", Decimal("0.001")),
    "ah": ("battery_capacity", Decimal("1")),
    "pa": ("pressure", Decimal("1")),
    "kpa": ("pressure", Decimal("1000")),
    "mpa": ("pressure", Decimal("1000000")),
    "bar": ("pressure", Decimal("100000")),
    "nm": ("torque", Decimal("1")),
    "hz": ("frequency", Decimal("1")),
}
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
    product_type_tokens: tuple[str, ...] = ()
    category_tokens: set[str] = field(default_factory=set)
    measurements: frozenset[str] = frozenset()
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
    current: str = ""
    torque: str = ""
    pressure: str = ""
    frequency: str = ""
    battery_count: int | None = None
    battery_capacity: str = ""
    is_bundle: bool = False


def extract_product_attributes(
    name: str,
    *,
    brand: str = "",
    model: str = "",
    category: str = "",
    description: str = "",
) -> ProductAttributes:
    normalized_name = normalize_product_name(name)
    normalized_category = normalize_product_name(category)
    normalized_description = normalize_product_name(description)
    name_tokens = set(tokenize(normalized_name))
    tokens = set(name_tokens)
    tokens.update(tokenize(normalized_category))
    tokens.update(tokenize(normalized_description))
    normalized_brand = _extract_brand(name_tokens, brand)
    normalized_model = _extract_model(normalized_name, model)
    searchable_details = " ".join(filter(None, (normalized_name, normalized_description)))
    dimensions = _extract_dimensions(searchable_details)
    measures = _extract_measures(searchable_details)
    measurements = frozenset(
        f"{value}{unit}"
        for value, unit in NUMBER_UNIT_RE.findall(searchable_details)
    )
    battery = BATTERY_RE.search(searchable_details)
    category_tokens = {
        _canonical_type_token(token)
        for token in tokenize(normalized_category)
        if _is_product_type_token(token, set(), "") and token not in GENERIC_CATEGORY_TOKENS
    }

    return ProductAttributes(
        normalized_name=normalized_name,
        tokens=tokens,
        product_type_tokens=_extract_product_type_tokens(
            normalized_name,
            normalized_brand,
            normalized_model,
        ),
        category_tokens=category_tokens,
        measurements=measurements,
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
        current=measures.get("current", ""),
        torque=measures.get("torque", ""),
        pressure=measures.get("pressure", ""),
        frequency=measures.get("frequency", ""),
        battery_count=int(battery.group(1)) if battery else None,
        battery_capacity=f"{battery.group(2)}ah" if battery else measures.get("battery_capacity", ""),
        is_bundle=_is_bundle(tokens, normalized_name),
    )


def canonical_measure(value: str) -> tuple[str, Decimal] | None:
    parsed = parse_measure_token(value)
    if not parsed:
        return None
    number, unit = parsed
    factor = MEASURE_FACTORS.get(unit)
    if not factor:
        return None
    kind, multiplier = factor
    try:
        return kind, Decimal(number) * multiplier
    except InvalidOperation:
        return None


def measures_equal(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_measure = canonical_measure(left)
    right_measure = canonical_measure(right)
    return bool(left_measure and right_measure and left_measure == right_measure)


def equivalent_measure_tokens(value: str) -> tuple[str, ...]:
    canonical = canonical_measure(value)
    if not canonical:
        return (value,) if value else ()
    kind, base_value = canonical
    result = []
    for unit, (candidate_kind, multiplier) in MEASURE_FACTORS.items():
        if candidate_kind != kind:
            continue
        converted = base_value / multiplier
        text = _decimal_text(converted)
        if text is not None:
            result.append(f"{text}{unit}")
    return tuple(dict.fromkeys(result))


def _decimal_text(value: Decimal) -> str | None:
    if value < 0 or value.as_tuple().exponent < -4:
        return None
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


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
        if unit in {"mg", "kg", "g"}:
            measures.setdefault("weight", normalized)
        elif unit in {"l", "cl", "ml"}:
            measures.setdefault("volume", normalized)
        elif unit == "v":
            measures.setdefault("voltage", normalized)
        elif unit in {"w", "kw"}:
            measures.setdefault("power", normalized)
        elif unit == "a":
            measures.setdefault("current", normalized)
        elif unit in {"ah", "mah"}:
            measures.setdefault("battery_capacity", normalized)
        elif unit in {"bar", "kpa", "mpa", "pa"}:
            measures.setdefault("pressure", normalized)
        elif unit == "nm":
            measures.setdefault("torque", normalized)
        elif unit == "hz":
            measures.setdefault("frequency", normalized)
        elif unit in {"mm", "cm", "m"}:
            measures.setdefault("length", normalized)
    return measures


def _extract_product_type_tokens(
    normalized_name: str,
    brand: str,
    model: str,
) -> tuple[str, ...]:
    ignored_tokens = set(tokenize(brand)) | set(tokenize(model)) | KNOWN_BRANDS
    result = []
    for token in tokenize(normalized_name):
        if not _is_product_type_token(token, ignored_tokens, model):
            continue
        canonical = _canonical_type_token(token)
        if canonical not in result:
            result.append(canonical)
        if len(result) >= 5:
            break
    return tuple(result)


def _is_product_type_token(token: str, ignored_tokens: set[str], model: str) -> bool:
    return bool(
        len(token) >= 3
        and token not in ignored_tokens
        and token not in PRODUCT_TYPE_STOP_WORDS
        and token != model
        and not is_number_token(token)
        and not parse_dimension_token(token)
        and not parse_measure_token(token)
        and not MODEL_RE.fullmatch(token)
        and not any(character.isdigit() for character in token)
    )


def _canonical_type_token(token: str) -> str:
    for suffix in ("ide", "ade", "ede", "id", "ad"):
        if len(token) >= 8 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _extract_quantity(normalized_name: str) -> int | None:
    match = PACK_RE.search(normalized_name)
    return int(match.group(1)) if match else None


def _is_bundle(tokens: set[str], normalized_name: str) -> bool:
    if tokens & BUNDLE_TOKENS:
        return True
    if "+" in normalized_name:
        return True
    return bool(BATTERY_RE.search(normalized_name))
