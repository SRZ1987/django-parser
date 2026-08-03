import asyncio
import logging
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
    ).select_related("product")
    if not retry_missing:
        offers = offers.filter(barcode_checked_at__isnull=True)

    targets = []
    for offer in offers.order_by("pk"):
        result.checked += 1
        if not offer.product_url:
            checked_at = timezone.now()
            ProductOffer.objects.filter(pk=offer.pk, barcode="").update(
                barcode_checked_at=checked_at,
                updated_at=checked_at,
            )
            result.not_found += 1
            continue
        targets.append((offer.pk, offer.sku or offer.external_id, offer.product_url))

    if targets:
        fetch_results = asyncio.run(_fetch_barcodes(targets, concurrency=max(1, int(concurrency))))
        offers_by_id = {
            offer.pk: offer
            for offer in ProductOffer.objects.filter(
                pk__in=[item.offer_id for item in fetch_results],
                shop__code=BAUHAUS_SHOP_CODE,
            ).select_related("product")
        }
        for item in fetch_results:
            offer = offers_by_id.get(item.offer_id)
            if offer is None or offer.barcode:
                continue

            ean = _validate_ean(item.ean)
            checked_at = timezone.now()
            try:
                with transaction.atomic():
                    if ean:
                        updated = ProductOffer.objects.filter(pk=offer.pk, barcode="").update(
                            barcode=ean,
                            barcode_checked_at=checked_at,
                            updated_at=checked_at,
                        )
                        if updated:
                            Product.objects.filter(pk=offer.product_id).update(barcode=ean, updated_at=checked_at)
                            result.found += 1
                    else:
                        ProductOffer.objects.filter(pk=offer.pk, barcode="").update(
                            barcode_checked_at=checked_at,
                            updated_at=checked_at,
                        )
                        if _is_fetch_error(item.source) or (item.ean and not ean):
                            result.errors += 1
                            _log(
                                f"WARNING: BAUHAUS barcode lookup failed for offer {offer.pk}: {item.source}",
                                log_callback,
                                warning=True,
                            )
                        else:
                            result.not_found += 1
            except Exception as exc:
                result.errors += 1
                try:
                    ProductOffer.objects.filter(pk=offer.pk, barcode="").update(
                        barcode_checked_at=checked_at,
                        updated_at=checked_at,
                    )
                except Exception as marker_exc:
                    _log(
                        f"WARNING: BAUHAUS barcode check marker failed for offer {offer.pk}: "
                        f"{type(marker_exc).__name__}: {marker_exc}",
                        log_callback,
                        warning=True,
                    )
                _log(
                    f"WARNING: BAUHAUS barcode update failed for offer {offer.pk}: "
                    f"{type(exc).__name__}: {exc}",
                    log_callback,
                    warning=True,
                )

    _log(result.summary(), log_callback)
    return result


async def _fetch_barcodes(targets, concurrency):
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
                    offer_id, sku, product_url = item
                    await controller.wait_ean_allowed()
                    _, ean, source, _ = await fetch_product_ean(
                        session,
                        ean_limiter,
                        controller,
                        sku,
                        product_url,
                    )
                    results.append(BarcodeFetchResult(offer_id=offer_id, ean=ean, source=source))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    results.append(
                        BarcodeFetchResult(
                            offer_id=item[0],
                            ean="",
                            source=f"worker_error:{type(exc).__name__}",
                        )
                    )
                finally:
                    queue.task_done()

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(concurrency, len(targets)))
        ]
        try:
            await queue.join()
        finally:
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


def _is_fetch_error(source):
    return source.startswith(("http_", "request_failed", "worker_error"))


def _validate_ean(value):
    text = str(value).strip() if value is not None else ""
    if not text.isdigit() or len(text) not in {8, 12, 13, 14}:
        return ""
    return normalize_barcode_candidate(text)


def _log(message, callback, warning=False):
    if warning:
        logger.warning(message)
    else:
        logger.info(message)
    if callback is not None:
        callback(message)
