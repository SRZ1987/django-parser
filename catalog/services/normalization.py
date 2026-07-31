import re
import unicodedata


_DASHES_RE = re.compile(r"[\u2010-\u2015\u2212\-]+")
_DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")
_MODEL_SPLIT_RE = re.compile(r"\b([a-z]{2,6})\s+(\d{2,5})([a-z0-9]*)\b")
_DIMENSION_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*[xх]\s*(\d+(?:\.\d+)?)(?:\s*[xх]\s*(\d+(?:\.\d+)?))?(?:\s*(mm|мм|cm|см|m|м)\b)?",
    re.IGNORECASE,
)
_NUMBER_UNIT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(mm|мм|cm|см|m|м|kg|кг|g|г|l|л|ml|мл|v|в|w|вт|ah|ач)\b",
    re.IGNORECASE,
)
_UNWANTED_CHARS_RE = re.compile(r"[^\w\s.]+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")
_UNIT_ALIASES = {
    "мм": "mm",
    "см": "cm",
    "м": "m",
    "кг": "kg",
    "г": "g",
    "л": "l",
    "мл": "ml",
    "в": "v",
    "вт": "w",
    "ач": "ah",
}


def _normalize_unit(unit: str) -> str:
    return _UNIT_ALIASES.get(unit.lower(), unit.lower())


def _normalize_dimension(match) -> str:
    values = [value for value in match.groups()[:3] if value]
    unit = _normalize_unit(match.group(4)) if match.group(4) else ""
    return "x".join(values) + unit


def _normalize_split_model(match) -> str:
    suffix = match.group(3)
    if suffix in {"v", "w", "ah", "mm", "cm", "m", "kg", "g", "l", "ml"}:
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}{suffix}"


def _compact_number_units(text: str) -> str:
    return _NUMBER_UNIT_RE.sub(lambda match: f"{match.group(1)}{_normalize_unit(match.group(2))}", text)


def normalize_text(value: str) -> str:
    if not value:
        return ""

    text = unicodedata.normalize("NFKC", str(value))
    text = text.lower().replace("ё", "е")
    text = text.replace("×", "x").replace("х", "x")
    text = _DECIMAL_COMMA_RE.sub(".", text)
    text = _DASHES_RE.sub(" ", text)
    text = _UNWANTED_CHARS_RE.sub(" ", text)
    text = _SPACES_RE.sub(" ", text)
    text = _MODEL_SPLIT_RE.sub(_normalize_split_model, text)
    text = _DIMENSION_RE.sub(_normalize_dimension, text)
    text = _SPACES_RE.sub(" ", text)
    return text.strip()


def normalize_product_name(value: str) -> str:
    return _compact_number_units(normalize_text(value))


def normalize_brand(value: str) -> str:
    return normalize_text(value)


def normalize_model(value: str) -> str:
    return normalize_product_name(value)


def tokenize(value: str) -> list[str]:
    return [token for token in normalize_product_name(value).split() if token]


def build_search_text(*values: str) -> str:
    normalized_values = [normalize_text(value) for value in values if value]
    return " ".join(value for value in normalized_values if value)
