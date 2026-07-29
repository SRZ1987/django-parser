import re
import unicodedata


_DASHES_RE = re.compile(r"[\u2010-\u2015\u2212\-]+")
_DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")
_UNWANTED_CHARS_RE = re.compile(r"[^\w\s.]+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    if not value:
        return ""

    text = unicodedata.normalize("NFKC", str(value))
    text = text.lower().replace("ё", "е")
    text = text.replace("×", "x")
    text = _DECIMAL_COMMA_RE.sub(".", text)
    text = _DASHES_RE.sub(" ", text)
    text = _UNWANTED_CHARS_RE.sub(" ", text)
    text = _SPACES_RE.sub(" ", text)
    return text.strip()


def normalize_product_name(value: str) -> str:
    return normalize_text(value)


def normalize_brand(value: str) -> str:
    return normalize_text(value)


def normalize_model(value: str) -> str:
    return normalize_text(value)


def build_search_text(*values: str) -> str:
    normalized_values = [normalize_text(value) for value in values if value]
    return " ".join(value for value in normalized_values if value)
