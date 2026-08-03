import re
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher

from catalog.models import ProductOffer

from .attribute_extraction import ProductAttributes, extract_product_attributes
from .normalization import (
    is_number_token,
    normalize_product_name,
    normalize_text,
    parse_dimension_token,
    tokenize,
)


MATCH_EXACT = "exact"
MATCH_SAME_PRODUCT = "same_product"
MATCH_BUNDLE_OR_VARIANT = "bundle_or_variant"
MATCH_SIMILAR_PRODUCT = "similar_product"

SAME_PRODUCT_SCORE = 0.46
BUNDLE_SCORE = 0.42
SIMILAR_SCORE = 0.12


@dataclass(frozen=True)
class MatchResult:
    offer: ProductOffer
    score: float
    match_type: str
    confidence: str
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PriceSummary:
    min_price: Decimal | None
    max_price: Decimal | None
    price_difference: Decimal | None
    cheapest_shop: str
    offers_count: int


def build_offer_attributes(offer: ProductOffer) -> ProductAttributes:
    return extract_product_attributes(
        offer.original_name,
        brand=offer.product.brand if offer.product_id and offer.product else "",
        model=offer.product.model if offer.product_id and offer.product else "",
    )


def score_offer_against_query(
    offer: ProductOffer,
    query: str,
    *,
    source_attributes: ProductAttributes | None = None,
) -> MatchResult:
    normalized_query = normalize_product_name(query)
    query_tokens = set(tokenize(normalized_query))
    offer_attributes = build_offer_attributes(offer)
    target_attributes = source_attributes or extract_product_attributes(query)
    score = 0.0
    reasons = []
    match_type = MATCH_SIMILAR_PRODUCT

    if normalized_query and _identifier_matches(offer, normalized_query):
        score = 1.0
        match_type = MATCH_EXACT
        reasons.append("exact barcode or shop code")

    if target_attributes.model and offer_attributes.model:
        if offer_attributes.model == target_attributes.model:
            score += 0.34
            reasons.append("same model")
        elif offer_attributes.base_model and offer_attributes.base_model == target_attributes.base_model:
            score += 0.26
            reasons.append("same base model")

    if target_attributes.brand and offer_attributes.brand:
        if offer_attributes.brand == target_attributes.brand:
            score += 0.16
            reasons.append("same brand")
        else:
            score -= 0.32
            reasons.append("different brand")

    token_score = _token_similarity(query_tokens or target_attributes.tokens, offer_attributes.tokens)
    if token_score:
        score += token_score * 0.28
        reasons.append("name tokens overlap")
    token_coverage = _token_coverage(query_tokens or target_attributes.tokens, offer_attributes.tokens)
    if token_coverage:
        score += token_coverage * 0.18
        reasons.append("query tokens covered")

    sequence_score = SequenceMatcher(None, normalized_query, offer_attributes.normalized_name).ratio() if normalized_query else 0
    if sequence_score >= 0.55:
        score += sequence_score * 0.14
        reasons.append("normalized name similarity")

    if _dimensions_match(target_attributes, offer_attributes):
        score += 0.12
        reasons.append("dimensions match")
    elif target_attributes.dimensions and offer_attributes.dimensions:
        score -= 0.06
        reasons.append("different dimensions")

    if _quantity_matches(target_attributes, offer_attributes):
        score += 0.08
        reasons.append("package quantity matches")
    elif target_attributes.quantity and offer_attributes.quantity:
        score -= 0.1
        reasons.append("different package quantity")

    if _bundle_relation(target_attributes, offer_attributes):
        score += 0.08
        reasons.append("bundle or kit variant")
        if match_type != MATCH_EXACT:
            match_type = MATCH_BUNDLE_OR_VARIANT

    structured_tokens = {
        token
        for token in query_tokens
        if is_number_token(token) or parse_dimension_token(token)
    }
    if (
        match_type != MATCH_EXACT
        and structured_tokens
        and structured_tokens == query_tokens
        and not all(_token_matches(token, offer_attributes.tokens) for token in structured_tokens)
    ):
        score = 0.0
        reasons.append("different number or dimensions")

    if match_type != MATCH_EXACT:
        if _bundle_relation(target_attributes, offer_attributes):
            match_type = MATCH_BUNDLE_OR_VARIANT
        elif score >= SAME_PRODUCT_SCORE:
            match_type = MATCH_SAME_PRODUCT
        else:
            match_type = MATCH_SIMILAR_PRODUCT

    score = max(0.0, min(score, 1.0))
    if match_type == MATCH_SIMILAR_PRODUCT and score < SIMILAR_SCORE:
        reasons.append("weak similarity")

    return MatchResult(
        offer=offer,
        score=round(score, 4),
        match_type=match_type,
        confidence=_confidence(score, match_type),
        reasons=reasons or ["candidate match"],
    )


def score_offer_against_offer(candidate: ProductOffer, source: ProductOffer) -> MatchResult:
    source_attributes = build_offer_attributes(source)
    result = score_offer_against_query(candidate, source.original_name, source_attributes=source_attributes)

    reasons = list(result.reasons)
    score = result.score
    match_type = result.match_type
    if source.barcode and candidate.barcode and source.barcode == candidate.barcode:
        score = 1.0
        match_type = MATCH_EXACT
        reasons.insert(0, "same barcode")
    elif source.sku and source.shop_id == candidate.shop_id and source.sku == candidate.sku:
        score = max(score, 0.98)
        match_type = MATCH_EXACT
        reasons.insert(0, "same shop code")

    if source.pk == candidate.pk:
        score = 1.0
        match_type = MATCH_EXACT
        reasons.insert(0, "selected offer")

    return MatchResult(
        offer=candidate,
        score=round(max(0.0, min(score, 1.0)), 4),
        match_type=match_type,
        confidence=_confidence(score, match_type),
        reasons=_unique_reasons(reasons),
    )


def build_price_summary(matches: list[MatchResult]) -> PriceSummary:
    priced = [(match.offer.current_price, match.offer.shop.name) for match in matches if match.offer.current_price is not None]
    if not priced:
        return PriceSummary(None, None, None, "", len(matches))

    min_price, cheapest_shop = min(priced, key=lambda item: item[0])
    max_price = max(price for price, _shop in priced)
    return PriceSummary(
        min_price=min_price,
        max_price=max_price,
        price_difference=max_price - min_price,
        cheapest_shop=cheapest_shop,
        offers_count=len(matches),
    )


def _identifier_matches(offer: ProductOffer, normalized_query: str) -> bool:
    identifiers = {
        normalize_text(offer.barcode),
        normalize_text(offer.product.barcode if offer.product_id and offer.product else ""),
        normalize_text(offer.sku),
        normalize_text(offer.external_id),
    }
    return normalized_query in identifiers


def _token_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = sum(1 for token in left if _token_matches(token, right))
    union = len(left | right)
    return intersection / union if union else 0.0


def _token_coverage(query_tokens: set[str], offer_tokens: set[str]) -> float:
    if not query_tokens or not offer_tokens:
        return 0.0
    return sum(1 for token in query_tokens if _token_matches(token, offer_tokens)) / len(query_tokens)


def _token_matches(query_token: str, offer_tokens: set[str]) -> bool:
    if query_token in offer_tokens:
        return True
    query_dimensions = parse_dimension_token(query_token)
    if query_dimensions:
        return any(
            candidate_dimensions[:len(query_dimensions)] == query_dimensions
            for offer_token in offer_tokens
            if (candidate_dimensions := parse_dimension_token(offer_token))
        )
    if is_number_token(query_token):
        number_pattern = re.compile(rf"(?<![\d.]){re.escape(query_token)}(?![\d.])")
        return any(number_pattern.search(offer_token) for offer_token in offer_tokens)
    if len(query_token) < 4:
        return any(
            offer_token.startswith(query_token)
            and any(character.isdigit() for character in offer_token)
            for offer_token in offer_tokens
        )
    return any(query_token in offer_token for offer_token in offer_tokens)


def _dimensions_match(source: ProductAttributes, candidate: ProductAttributes) -> bool:
    return bool(
        source.dimensions
        and candidate.dimensions
        and candidate.dimensions[:len(source.dimensions)] == source.dimensions
    )


def _quantity_matches(source: ProductAttributes, candidate: ProductAttributes) -> bool:
    return bool(source.quantity and candidate.quantity and source.quantity == candidate.quantity)


def _bundle_relation(source: ProductAttributes, candidate: ProductAttributes) -> bool:
    if not (source.is_bundle or candidate.is_bundle):
        return False
    same_model = source.base_model and source.base_model == candidate.base_model
    shared_tokens = source.tokens & candidate.tokens
    return bool(same_model or len(shared_tokens) >= 2)


def _confidence(score: float, match_type: str) -> str:
    if match_type == MATCH_EXACT:
        return "exact"
    if score >= SAME_PRODUCT_SCORE:
        return "high"
    if score >= BUNDLE_SCORE:
        return "medium"
    return "low"


def _unique_reasons(reasons: list[str]) -> list[str]:
    seen = set()
    unique = []
    for reason in reasons:
        if reason not in seen:
            unique.append(reason)
            seen.add(reason)
    return unique
