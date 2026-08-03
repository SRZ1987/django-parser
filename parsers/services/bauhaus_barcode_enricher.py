import asyncio
import logging
import queue
import threading
import time
from dataclasses import dataclass

from curl_cffi.requests import AsyncSession as CurlAsyncSession
from django.db import transaction
from django.utils import timezone

from catalog.models import Product, ProductOffer
from parsers.standalone.bauhaus_parser import (
    EAN_CONCURRENCY,
    HEADERS,
    AdaptiveLoadController,
    AdjustableLimiter,
    fetch_product_ean,
    normalize_barcode_candidate,
)


logger = logging.getLogger(__name__)

BAUHAUS_SHOP_CODE = "bauhaus"
DEFAULT_CONCURRENCY = 8
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

    def summary(self):
        return (
            "BAUHAUS barcode enrichment: "
            f"checked={self.checked}, found={self.found}, "
            f"not_found={self.not_found}, errors={self.errors}"
        )


@dataclass(frozen=True)
class BarcodeFetchResult:
    offer_id: int
    ean: str
    source: str


@dataclass(frozen=True)
class BarcodeFetchFailure:
    error: BaseException


def enrich_bauhaus_offer_barcodes(
    offer_ids,
    *,
    retry_missing=False,
    log_callback=None,
    concurrency=DEFAULT_CONCURRENCY,
):
    unique_ids = list(dict.fromkeys(int(offer_id) for offer_id in offer_ids))
    result = BauhausBarcodeEnrichmentResult()
    if not unique_ids:
        _log(result.summary(), log_callback)
        return result

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

        _log(
            "BAUHAUS barcode progress: "
            f"checked={result.checked}/{total}, found={result.found}, "
            f"not_found={result.not_found}, errors={result.errors}, "
            f"remaining={max(total - result.checked, 0)}, "
            f"elapsed={_format_elapsed(now - started_at)}",
            log_callback,
        )
        last_progress_at = now
        last_progress_count = result.checked

    _log(
        f"BAUHAUS barcode enrichment started: total={total}, "
        f"concurrency={max(1, int(concurrency))}",
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
        targets.append((offer.pk, offer.sku or offer.external_id, offer.product_url))

    if targets:
        try:
            for item in _iter_fetch_results(
                targets,
                concurrency=max(1, int(concurrency)),
                idle_callback=log_progress,
            ):
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


def _iter_fetch_results(targets, concurrency, *, idle_callback=None):
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


async def _fetch_barcodes(
    targets,
    concurrency,
    *,
    result_callback=None,
    stop_event=None,
):
    queue = asyncio.Queue()
    for target in targets:
        queue.put_nowait(target)

    page_limiter = AdjustableLimiter(1)
    ean_limiter = AdjustableLimiter(EAN_CONCURRENCY)
    controller = AdaptiveLoadController(queue, page_limiter, ean_limiter)
    results = []

    async with CurlAsyncSession(
        headers=HEADERS,
        impersonate="chrome",
        max_clients=concurrency,
    ) as session:

        async def worker():
            while True:
                item = await queue.get()
                try:
                    try:
                        offer_id, sku, product_url = item
                        await controller.wait_ean_allowed()
                        _, ean, source, _ = await fetch_product_ean(
                            session,
                            ean_limiter,
                            controller,
                            sku,
                            product_url,
                        )
                        fetch_result = BarcodeFetchResult(
                            offer_id=offer_id,
                            ean=ean,
                            source=source,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        fetch_result = BarcodeFetchResult(
                            offer_id=item[0],
                            ean="",
                            source=f"worker_error:{type(exc).__name__}",
                        )

                    if result_callback is None:
                        results.append(fetch_result)
                    else:
                        result_callback(fetch_result)
                finally:
                    queue.task_done()

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(concurrency, len(targets)))
        ]
        join_task = asyncio.create_task(queue.join())
        stop_task = None
        if stop_event is not None:
            stop_task = asyncio.create_task(_wait_for_stop(stop_event))
        try:
            if stop_task is None:
                await join_task
            else:
                done, _ = await asyncio.wait(
                    {join_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    raise asyncio.CancelledError
                await join_task
        finally:
            join_task.cancel()
            if stop_task is not None:
                stop_task.cancel()
            await asyncio.gather(
                *[task for task in (join_task, stop_task) if task is not None],
                return_exceptions=True,
            )
            for worker_task in workers:
                worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            background_tasks = [
                task
                for task in (controller._drain_task, controller._speed_pause_task)
                if task is not None and not task.done()
            ]
            for task in background_tasks:
                task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)

    return results


async def _wait_for_stop(stop_event):
    while not stop_event.is_set():
        await asyncio.sleep(0.25)


def _persist_fetch_result(item, *, product_id):
    ean = _validate_ean(item.ean)
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
            if _is_fetch_error(item.source) or (item.ean and not ean):
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


def _is_fetch_error(source):
    return source.startswith(("http_", "request_failed", "worker_error"))


def _validate_ean(value):
    text = str(value).strip() if value is not None else ""
    if not text.isdigit() or len(text) not in {8, 12, 13, 14}:
        return ""
    return normalize_barcode_candidate(text)


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
