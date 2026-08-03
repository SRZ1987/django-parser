import re
from dataclasses import dataclass, field

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q

from catalog.models import ProductOffer

from .attribute_extraction import extract_product_attributes
from .normalization import (
    is_number_token,
    normalize_product_name,
    normalize_text,
    parse_dimension_token,
    tokenize,
)
from .product_matching import (
    MATCH_BUNDLE_OR_VARIANT,
    MATCH_EXACT,
    MATCH_SAME_PRODUCT,
    MATCH_SIMILAR_PRODUCT,
    MatchResult,
    PriceSummary,
    build_offer_attributes,
    build_price_summary,
    score_offer_against_offer,
    score_offer_against_query,
)


DEFAULT_CANDIDATE_LIMIT = 350
DEFAULT_RESULTS_LIMIT = 120
DEFAULT_PAGE_SIZE = 24


@dataclass
class SearchResults:
    query: str
    normalized_query: str
    exact_matches: list[MatchResult] = field(default_factory=list)
    same_product: list[MatchResult] = field(default_factory=list)
    bundles_or_variants: list[MatchResult] = field(default_factory=list)
    similar_products: list[MatchResult] = field(default_factory=list)
    price_summary: PriceSummary | None = None
    total_count: int = 0
    candidates_count: int = 0

    @property
    def has_results(self) -> bool:
        return bool(self.total_count)


def available_offer_queryset():
    return ProductOffer.objects.filter(is_active=True, is_available=True).select_related("shop", "category", "product")


def search_products(
    query: str,
    *,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    results_limit: int = DEFAULT_RESULTS_LIMIT,
) -> SearchResults:
    query = (query or "").strip()
    normalized_query = normalize_product_name(query)
    if not normalized_query:
        return SearchResults(query=query, normalized_query=normalized_query)

    source_offer = _find_source_offer(query, normalized_query)
    source_attributes = build_offer_attributes(source_offer) if source_offer else extract_product_attributes(query)
    candidates = _retrieve_candidates(normalized_query, source_offer, source_attributes, candidate_limit)
    ranked = [
        score_offer_against_offer(candidate, source_offer)
        if source_offer
        else score_offer_against_query(candidate, query, source_attributes=source_attributes)
        for candidate in candidates
    ]
    ranked = [match for match in ranked if match.score >= 0.05 or match.match_type == MATCH_EXACT]
    ranked.sort(
        key=lambda match: (
            -match.ranking_tier,
            -match.score,
            _price_sort_value(match),
            match.offer.original_name.casefold(),
            match.offer.shop_id,
            match.offer.pk,
        )
    )
    ranked = ranked[:results_limit]

    results = SearchResults(
        query=query,
        normalized_query=normalized_query,
        candidates_count=len(candidates),
    )
    for match in ranked:
        _append_match(results, match)

    same_product_for_price = results.exact_matches + results.same_product
    results.price_summary = build_price_summary(same_product_for_price) if same_product_for_price else None
    results.total_count = sum(
        len(group)
        for group in (
            results.exact_matches,
            results.same_product,
            results.bundles_or_variants,
            results.similar_products,
        )
    )
    return results


def find_matches(offer_or_product, *, candidate_limit: int = DEFAULT_CANDIDATE_LIMIT) -> SearchResults:
    offer = offer_or_product
    if not isinstance(offer, ProductOffer):
        offer = ProductOffer.objects.filter(product=offer_or_product, is_active=True, is_available=True).first()
    if offer is None:
        return SearchResults(query="", normalized_query="")
    return search_products(offer.sku or offer.barcode or offer.original_name, candidate_limit=candidate_limit)


def paginate_group(items, page_number, *, page_size: int = DEFAULT_PAGE_SIZE):
    paginator = Paginator(items, page_size)
    try:
        return paginator.page(page_number)
    except PageNotAnInteger:
        return paginator.page(1)
    except EmptyPage:
        return paginator.page(paginator.num_pages)


def _find_source_offer(raw_query: str, normalized_query: str) -> ProductOffer | None:
    return (
        available_offer_queryset()
        .filter(
            Q(barcode__iexact=raw_query)
            | Q(barcode__iexact=normalized_query)
            | Q(product__barcode__iexact=raw_query)
            | Q(product__barcode__iexact=normalized_query)
            | Q(sku__iexact=raw_query)
            | Q(sku__iexact=normalized_query)
            | Q(external_id__iexact=raw_query)
            | Q(external_id__iexact=normalized_query)
        )
        .order_by("shop__name", "original_name")
        .first()
    )


def _retrieve_candidates(normalized_query, source_offer, source_attributes, candidate_limit):
    queryset = available_offer_queryset()
    broad_query = Q()
    tokens = tokenize(normalized_query)
    meaningful_tokens = [token for token in tokens if len(token) >= 2][:6]
    candidates = []
    seen_ids = set()

    if normalized_query:
        exact_query = Q(barcode__iexact=normalized_query) | Q(product__barcode__iexact=normalized_query)
        exact_query |= Q(sku__iexact=normalized_query) | Q(external_id__iexact=normalized_query)
        _extend_candidates(candidates, seen_ids, queryset.filter(exact_query), candidate_limit)

        broad_query |= exact_query
        if is_number_token(normalized_query) or parse_dimension_token(normalized_query):
            broad_query |= _token_candidate_query(normalized_query)
        else:
            broad_query |= Q(normalized_name__icontains=normalized_query) | Q(search_text__icontains=normalized_query)

    if meaningful_tokens:
        all_tokens_query = Q()
        for token in meaningful_tokens:
            all_tokens_query &= _token_candidate_query(token)
        _extend_candidates(candidates, seen_ids, queryset.filter(all_tokens_query), candidate_limit)

    if source_offer:
        if source_offer.barcode:
            broad_query |= Q(barcode=source_offer.barcode) | Q(product__barcode=source_offer.barcode)
        if source_offer.sku:
            broad_query |= Q(sku=source_offer.sku, shop=source_offer.shop)

    if source_attributes.brand:
        broad_query |= Q(product__normalized_brand=source_attributes.brand) | Q(search_text__icontains=source_attributes.brand)
    if source_attributes.model:
        broad_query |= Q(product__normalized_model=source_attributes.model) | Q(search_text__icontains=source_attributes.model)
    if source_attributes.base_model and source_attributes.base_model != source_attributes.model:
        broad_query |= Q(search_text__icontains=source_attributes.base_model)
    for dimension in source_attributes.dimensions:
        broad_query |= Q(search_text__icontains=dimension)

    for token in meaningful_tokens:
        broad_query |= _token_candidate_query(token)

    if broad_query and len(candidates) < candidate_limit:
        _extend_candidates(candidates, seen_ids, queryset.filter(broad_query), candidate_limit)

    return candidates


def _token_candidate_query(token: str) -> Q:
    dimensions = parse_dimension_token(token)
    if dimensions:
        dimension = "x".join(re.escape(value) for value in dimensions)
        suffix = r"(x[0-9]|[^0-9.]|$)" if len(dimensions) < 3 else r"([^0-9.]|$)"
        pattern = rf"(^|[^0-9.]){dimension}{suffix}"
        return Q(search_text__regex=pattern) | Q(normalized_name__regex=pattern)

    if is_number_token(token):
        pattern = rf"(^|[^0-9.]){re.escape(token)}([^0-9.]|$)"
        return Q(search_text__regex=pattern) | Q(normalized_name__regex=pattern)

    pattern = rf"(^|[^\w])\w*{re.escape(token)}([^\w]|$)"
    return Q(search_text__iregex=pattern) | Q(normalized_name__iregex=pattern)


def _extend_candidates(candidates, seen_ids, queryset, candidate_limit):
    for offer in queryset.distinct().order_by("shop__name", "original_name", "id")[:candidate_limit]:
        if offer.pk in seen_ids:
            continue
        candidates.append(offer)
        seen_ids.add(offer.pk)
        if len(candidates) >= candidate_limit:
            break


def _append_match(results: SearchResults, match: MatchResult):
    if match.match_type == MATCH_EXACT:
        results.exact_matches.append(match)
    elif match.match_type == MATCH_SAME_PRODUCT:
        results.same_product.append(match)
    elif match.match_type == MATCH_BUNDLE_OR_VARIANT:
        results.bundles_or_variants.append(match)
    else:
        results.similar_products.append(match)


def _price_sort_value(match: MatchResult):
    return match.offer.current_price if match.offer.current_price is not None else 999999999
