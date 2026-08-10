import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Case, IntegerField, Q, Value, When

from catalog.models import ProductOffer

from .attribute_extraction import extract_product_attributes
from .normalization import (
    allows_token_prefix,
    is_number_token,
    is_meaningful_query_token,
    normalize_product_name,
    normalize_text,
    parse_dimension_token,
    parse_measure_token,
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
    matches: list[MatchResult] = field(default_factory=list)
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
    ranked.sort(key=lambda match: _match_sort_key(match, identifier_search=source_offer is not None))
    ranked = ranked[:results_limit]

    results = SearchResults(
        query=query,
        normalized_query=normalized_query,
        matches=ranked,
        candidates_count=len(candidates),
    )
    for match in ranked:
        _append_match(results, match)

    price_matches = results.exact_matches + results.same_product if source_offer else results.matches
    results.price_summary = build_price_summary(price_matches) if price_matches else None
    results.total_count = len(ranked)
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
        .annotate(
            identifier_priority=Case(
                When(
                    Q(barcode__iexact=raw_query)
                    | Q(barcode__iexact=normalized_query)
                    | Q(product__barcode__iexact=raw_query)
                    | Q(product__barcode__iexact=normalized_query),
                    then=Value(0),
                ),
                When(
                    Q(sku__iexact=raw_query) | Q(sku__iexact=normalized_query),
                    then=Value(1),
                ),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("identifier_priority", "shop__name", "original_name")
        .first()
    )


def _retrieve_candidates(normalized_query, source_offer, source_attributes, candidate_limit):
    queryset = available_offer_queryset()
    broad_query = Q()
    tokens = tokenize(normalized_query)
    meaningful_tokens = [token for token in tokens if is_meaningful_query_token(token)]
    candidates = []
    seen_ids = set()

    if normalized_query:
        exact_query = Q(barcode__iexact=normalized_query) | Q(product__barcode__iexact=normalized_query)
        exact_query |= Q(sku__iexact=normalized_query) | Q(external_id__iexact=normalized_query)
        _extend_candidates(candidates, seen_ids, queryset.filter(exact_query), candidate_limit)

        broad_query |= exact_query
        if is_number_token(normalized_query) or parse_dimension_token(normalized_query):
            broad_query |= build_token_candidate_query(normalized_query)
        else:
            broad_query |= Q(normalized_name__icontains=normalized_query) | Q(search_text__icontains=normalized_query)

    if meaningful_tokens:
        all_tokens_query = Q()
        for token in meaningful_tokens:
            all_tokens_query &= build_token_candidate_query(token)
        _extend_candidates(candidates, seen_ids, queryset.filter(all_tokens_query), candidate_limit)

    if len(meaningful_tokens) >= 2 and source_offer is None:
        return candidates

    if source_offer:
        if source_offer.barcode:
            barcode_query = Q(barcode=source_offer.barcode) | Q(product__barcode=source_offer.barcode)
            _extend_candidates(candidates, seen_ids, queryset.filter(barcode_query), candidate_limit)
            broad_query |= barcode_query
        if source_offer.sku:
            broad_query |= Q(sku=source_offer.sku, shop=source_offer.shop)

        source_name_query = Q(normalized_name__iexact=source_attributes.normalized_name)
        source_name_query |= Q(product__normalized_name__iexact=source_attributes.normalized_name)
        _extend_candidates(candidates, seen_ids, queryset.filter(source_name_query), candidate_limit)
        broad_query |= source_name_query

        source_name_tokens = [
            token
            for token in tokenize(source_attributes.normalized_name)
            if is_meaningful_query_token(token)
        ][:6]
        if source_name_tokens:
            source_all_tokens_query = Q()
            for token in source_name_tokens:
                source_all_tokens_query &= build_token_candidate_query(token)
            _extend_candidates(
                candidates,
                seen_ids,
                queryset.filter(source_all_tokens_query),
                candidate_limit,
            )
            for token in source_name_tokens:
                broad_query |= build_token_candidate_query(token)

    if source_attributes.brand:
        broad_query |= Q(product__normalized_brand=source_attributes.brand) | Q(search_text__icontains=source_attributes.brand)
    if source_attributes.model:
        broad_query |= Q(product__normalized_model=source_attributes.model) | Q(search_text__icontains=source_attributes.model)
    if source_attributes.base_model and source_attributes.base_model != source_attributes.model:
        broad_query |= Q(search_text__icontains=source_attributes.base_model)
    for dimension in source_attributes.dimensions:
        broad_query |= Q(search_text__icontains=dimension)

    for token in meaningful_tokens:
        broad_query |= build_token_candidate_query(token)

    if broad_query and len(candidates) < candidate_limit:
        _extend_candidates(candidates, seen_ids, queryset.filter(broad_query), candidate_limit)

    return candidates


def build_token_candidate_query(token: str) -> Q:
    dimensions = parse_dimension_token(token)
    if dimensions:
        dimension = "x".join(_number_regex(value) for value in dimensions)
        suffix = r"(x[0-9]|[^0-9.]|$)" if len(dimensions) < 3 else r"([^0-9.]|$)"
        pattern = rf"(^|[^0-9.]){dimension}{suffix}"
        return Q(search_text__regex=pattern) | Q(normalized_name__regex=pattern)

    measure = parse_measure_token(token)
    if measure:
        number, unit = measure
        pattern = rf"(^|[^0-9.]){_number_regex(number)}\s*{re.escape(unit)}([^a-z0-9]|$)"
        return Q(search_text__iregex=pattern) | Q(normalized_name__iregex=pattern)

    if is_number_token(token):
        pattern = rf"(^|[^0-9.]){_number_regex(token)}([^0-9.]|$)"
        return Q(search_text__regex=pattern) | Q(normalized_name__regex=pattern)

    escaped_token = re.escape(token)
    if allows_token_prefix(token):
        pattern = rf"(^|[^\w])(?:\w*{escaped_token}|{escaped_token}\w*)([^\w]|$)"
    else:
        pattern = rf"(^|[^\w])\w*{escaped_token}([^\w]|$)"
    return Q(search_text__iregex=pattern) | Q(normalized_name__iregex=pattern)


def _number_regex(value: str) -> str:
    integer, separator, fraction = value.partition(".")
    if separator:
        return rf"{re.escape(integer)}[.]{re.escape(fraction)}0*"
    return rf"{re.escape(integer)}(?:[.]0+)?"


def _extend_candidates(candidates, seen_ids, queryset, candidate_limit):
    for offer in queryset.distinct().order_by("original_name", "id")[:candidate_limit]:
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


def _match_sort_key(match: MatchResult, *, identifier_search: bool):
    price = _effective_price(match.offer)
    stable_tail = (
        match.offer.original_name.casefold(),
        match.offer.shop.name.casefold(),
        match.offer.pk,
    )
    if not identifier_search:
        return (
            price is None,
            price if price is not None else Decimal("0"),
            -match.ranking_tier,
            -match.score,
            *stable_tail,
        )
    return (
        match.match_type != MATCH_EXACT,
        -match.ranking_tier,
        price is None,
        price if price is not None else Decimal("0"),
        *stable_tail,
    )


def _effective_price(offer: ProductOffer):
    if offer.sale_price is not None and (offer.price is None or offer.sale_price < offer.price):
        return offer.sale_price
    return offer.price
