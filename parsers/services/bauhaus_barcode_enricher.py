import asyncio
import logging
import queue
import threading
import time
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from catalog.models import Product, ProductOffer
from parsers.models import ParserConfig
from parsers.services.bauhaus_barcode_client import (
    BarcodeFetchMetrics,
    BarcodeFetchResult,
    BarcodeTarget,
    FastAdaptiveController,
    ProductPageResult,
    extract_related_product_eans,
    fetch_barcodes as _fetch_barcodes,
    fetch_product_page as _fetch_product_page,
    is_fetch_error,
    normalize_product_url,
    validate_ean,
)
from parsers.standalone.bauhaus_parser import clean_text


logger = logging.getLogger(__name__)

BAUHAUS_SHOP_CODE = "bauhaus"
RUNTIME_SETTINGS_KEY = "bauhaus_barcode_enrichment"
PROGRESS_EVERY_ITEMS = 100
PROGRESS_INTERVAL_SECONDS = 15
RESULT_QUEUE_SIZE_MULTIPLIER = 4

_FETCH_COMPLETE = object()


@dataclass
class BauhausBarcodeEnrichmentResult:
    checked: int = 0
    found: int = 0
    not_found: int = 0
    errors: int = 0
    pages: int = 0
    http_requests: int = 0
    related_found: int = 0
    restrictions: int = 0
    start_concurrency: int = 0
    final_concurrency: int = 0

    def summary(self):
        return (
            "BAUHAUS barcode enrichment: "
            f"checked={self.checked}, found={self.found}, "
            f"not_found={self.not_found}, errors={self.errors}, "
            f"pages={self.pages}, requests={self.http_requests}, "
            f"related={self.related_found}, restrictions={self.restrictions}, "
            f"concurrency={self.start_concurrency}->{self.final_concurrency}"
        )


@dataclass(frozen=True)
class BarcodeFetchFailure:
    error: BaseException


def enrich_bauhaus_offer_barcodes(
    offer_ids,
    *,
    retry_missing=False,
    log_callback=None,
    concurrency=None,
    retune=False,
):
    unique_ids = list(dict.fromkeys(int(offer_id) for offer_id in offer_ids))
    result = BauhausBarcodeEnrichmentResult()
    if not unique_ids:
        _log(result.summary(), log_callback)
        return result

    max_concurrency = max(
        1,
        int(getattr(settings, "BAUHAUS_BARCODE_MAX_CONCURRENCY", 200)),
    )
    minimum = max(
        1,
        min(
            max_concurrency,
            int(getattr(settings, "BAUHAUS_BARCODE_MIN_CONCURRENCY", 8)),
        ),
    )
    saved_concurrency = None if retune else _load_saved_concurrency()
    if concurrency is not None:
        start_concurrency = int(concurrency)
    elif saved_concurrency is not None:
        start_concurrency = saved_concurrency
    else:
        start_concurrency = max_concurrency
    start_concurrency = max(minimum, min(max_concurrency, start_concurrency))
    result.start_concurrency = start_concurrency
    result.final_concurrency = start_concurrency

    offers = ProductOffer.objects.filter(
        pk__in=unique_ids,
        shop__code=BAUHAUS_SHOP_CODE,
        barcode="",
    ).only(
        "pk",
        "product_id",
        "sku",
        "external_id",
        "product_url",
        "barcode_checked_at",
    )
    if not retry_missing:
        offers = offers.filter(barcode_checked_at__isnull=True)

    targets = []
    product_ids = {}
    eligible_offers = list(offers.order_by("pk"))
    total = len(eligible_offers)
    started_at = time.monotonic()
    last_progress_at = started_at
    last_progress_count = 0

    def log_progress(*, force=False):
        nonlocal last_progress_at, last_progress_count
        now = time.monotonic()
        if not force:
            enough_items = result.checked - last_progress_count >= PROGRESS_EVERY_ITEMS
            enough_time = now - last_progress_at >= PROGRESS_INTERVAL_SECONDS
            if not enough_items and not enough_time:
                return

        elapsed = now - started_at
        rate = result.checked / elapsed if elapsed > 0 else 0.0
        remaining = max(total - result.checked, 0)
        eta = remaining / rate if rate > 0 else 0
        _log(
            "BAUHAUS barcode progress: "
            f"checked={result.checked}/{total}, found={result.found}, "
            f"not_found={result.not_found}, errors={result.errors}, "
            f"pages={result.pages}, requests={result.http_requests}, "
            f"related={result.related_found}, concurrency={result.final_concurrency}, "
            f"restrictions={result.restrictions}, remaining={remaining}, "
            f"speed={rate:.1f}/s, elapsed={_format_elapsed(elapsed)}, "
            f"eta={_format_elapsed(eta)}",
            log_callback,
        )
        last_progress_at = now
        last_progress_count = result.checked

    source = "explicit"
    if concurrency is None:
        source = "saved" if saved_concurrency is not None else "maximum"
    _log(
        f"BAUHAUS barcode enrichment started: total={total}, "
        f"concurrency={start_concurrency} ({source}), range={minimum}-{max_concurrency}",
        log_callback,
    )

    for offer in eligible_offers:
        if not offer.product_url:
            checked_at = timezone.now()
            ProductOffer.objects.filter(pk=offer.pk, barcode="").update(
                barcode_checked_at=checked_at,
                updated_at=checked_at,
            )
            result.checked += 1
            result.not_found += 1
            log_progress()
            continue
        product_ids[offer.pk] = offer.product_id
        sku = clean_text(offer.sku or offer.external_id)
        targets.append(
            BarcodeTarget(
                offer_id=offer.pk,
                sku=sku,
                product_url=offer.product_url,
                key=(sku, normalize_product_url(offer.product_url)),
            )
        )

    if targets:
        try:
            for item in _iter_fetch_results(
                targets,
                concurrency=start_concurrency,
                minimum=minimum,
                idle_callback=log_progress,
            ):
                if isinstance(item, BarcodeFetchMetrics):
                    result.pages += item.pages
                    result.http_requests += item.http_requests
                    result.related_found += item.related_found
                    if item.restrictions is not None:
                        result.restrictions = item.restrictions
                    if item.concurrency is not None:
                        result.final_concurrency = item.concurrency
                    if item.persist_tuning:
                        _save_tuning_settings(
                            concurrency=result.final_concurrency,
                            restrictions=result.restrictions,
                            stable=item.stable,
                        )
                    if item.message:
                        _log(item.message, log_callback, warning=True)
                    log_progress()
                    continue

                outcome, warning = _persist_fetch_result(
                    item,
                    product_id=product_ids[item.offer_id],
                )
                result.checked += 1
                if outcome == "found":
                    result.found += 1
                elif outcome == "not_found":
                    result.not_found += 1
                elif outcome == "error":
                    result.errors += 1

                if warning:
                    _log(warning, log_callback, warning=True)
                log_progress()
        except BaseException:
            log_progress(force=True)
            raise

    log_progress(force=True)
    _log(result.summary(), log_callback)
    return result


def _iter_fetch_results(
    targets,
    concurrency,
    *,
    minimum=1,
    idle_callback=None,
):
    result_queue = queue.Queue(
        maxsize=max(concurrency * RESULT_QUEUE_SIZE_MULTIPLIER, concurrency),
    )
    stop_event = threading.Event()

    def put_result(item):
        while not stop_event.is_set():
            try:
                result_queue.put(item, timeout=0.25)
                return
            except queue.Full:
                continue

    def run_fetch():
        try:
            asyncio.run(
                _fetch_barcodes(
                    targets,
                    concurrency,
                    minimum=minimum,
                    result_callback=put_result,
                    stop_event=stop_event,
                )
            )
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            put_result(BarcodeFetchFailure(exc))
        finally:
            put_result(_FETCH_COMPLETE)

    fetch_thread = threading.Thread(
        target=run_fetch,
        name="bauhaus-barcode-fetch",
        daemon=True,
    )
    fetch_thread.start()
    try:
        while True:
            try:
                item = result_queue.get(timeout=0.5)
            except queue.Empty:
                if idle_callback is not None:
                    idle_callback()
                if fetch_thread.is_alive():
                    continue
                break

            if item is _FETCH_COMPLETE:
                break
            if isinstance(item, BarcodeFetchFailure):
                raise item.error
            yield item
    finally:
        stop_event.set()
        fetch_thread.join(timeout=2)


def _persist_fetch_result(item, *, product_id):
    ean = validate_ean(item.ean)
    checked_at = timezone.now()
    try:
        with transaction.atomic():
            if ean:
                updated = ProductOffer.objects.filter(
                    pk=item.offer_id,
                    barcode="",
                ).update(
                    barcode=ean,
                    barcode_checked_at=checked_at,
                    updated_at=checked_at,
                )
                if not updated:
                    return "skipped", ""
                Product.objects.filter(pk=product_id).update(
                    barcode=ean,
                    updated_at=checked_at,
                )
                return "found", ""

            updated = ProductOffer.objects.filter(
                pk=item.offer_id,
                barcode="",
            ).update(
                barcode_checked_at=checked_at,
                updated_at=checked_at,
            )
            if not updated:
                return "skipped", ""
            if is_fetch_error(item.source) or (item.ean and not ean):
                return (
                    "error",
                    f"WARNING: BAUHAUS barcode lookup failed for offer "
                    f"{item.offer_id}: {item.source}",
                )
            return "not_found", ""
    except Exception as exc:
        marker_warning = ""
        try:
            ProductOffer.objects.filter(
                pk=item.offer_id,
                barcode="",
            ).update(
                barcode_checked_at=checked_at,
                updated_at=checked_at,
            )
        except Exception as marker_exc:
            marker_warning = (
                f"; marker failed: {type(marker_exc).__name__}: {marker_exc}"
            )
        return (
            "error",
            f"WARNING: BAUHAUS barcode update failed for offer {item.offer_id}: "
            f"{type(exc).__name__}: {exc}{marker_warning}",
        )


def _load_saved_concurrency():
    runtime_settings = (
        ParserConfig.objects.filter(code=BAUHAUS_SHOP_CODE)
        .values_list("runtime_settings", flat=True)
        .first()
    )
    if not isinstance(runtime_settings, dict):
        return None
    value = runtime_settings.get(RUNTIME_SETTINGS_KEY, {}).get("concurrency")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _save_tuning_settings(*, concurrency, restrictions, stable):
    parser_config = ParserConfig.objects.filter(code=BAUHAUS_SHOP_CODE).first()
    if parser_config is None:
        return
    runtime_settings = dict(parser_config.runtime_settings or {})
    runtime_settings[RUNTIME_SETTINGS_KEY] = {
        "concurrency": int(concurrency),
        "restrictions": int(restrictions),
        "stable": bool(stable),
        "updated_at": timezone.now().isoformat(),
    }
    ParserConfig.objects.filter(pk=parser_config.pk).update(
        runtime_settings=runtime_settings,
        updated_at=timezone.now(),
    )


def _format_elapsed(seconds):
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _log(message, callback, warning=False):
    if warning:
        logger.warning(message)
    else:
        logger.info(message)
    if callback is not None:
        callback(message)
