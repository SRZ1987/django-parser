import asyncio
import json
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from curl_cffi.requests import AsyncSession as CurlAsyncSession
from django.conf import settings

from parsers.standalone.bauhaus_parser import (
    BASE_URL,
    EAN_MAX_RETRIES,
    EAN_REQUEST_TIMEOUT,
    HEADERS,
    AdjustableLimiter,
    barcode_from_mapping,
    clean_text,
    document_versions,
    extract_deployment_id,
    extract_ean_from_jsonld,
    extract_ean_from_next_data,
    json_values_after_marker,
    normalize_barcode_candidate,
    walk_json,
)


ADAPTATION_COOLDOWN_SECONDS = 3
RELATED_DATA_MARKERS = (
    '"hits":',
    '"products":',
    '"items":',
    '"recommendations":',
    '"alternatives":',
)
JSON_STRING = r'((?:\\.|[^"\\])*)'
RELATED_RECORD_PATTERN = re.compile(
    rf'"sku"\s*:\s*"{JSON_STRING}"(?P<body>.*?)(?="sku"\s*:|$)',
    flags=re.DOTALL,
)
EAN_FIELD_PATTERN = re.compile(
    rf'"(?:gtin(?:8|12|13|14)?|ean(?:8|13|14)?|barcode)"\s*:\s*"{JSON_STRING}"',
    flags=re.IGNORECASE,
)
URL_FIELD_PATTERN = re.compile(
    rf'"(?:canonical_url|product_url|url)"\s*:\s*"{JSON_STRING}"',
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class BarcodeTarget:
    offer_id: int
    sku: str
    product_url: str
    key: tuple[str, str]


@dataclass(frozen=True)
class BarcodeFetchResult:
    offer_id: int
    ean: str
    source: str


@dataclass(frozen=True)
class BarcodeFetchMetrics:
    pages: int = 0
    http_requests: int = 0
    related_found: int = 0
    restrictions: int | None = None
    concurrency: int | None = None
    persist_tuning: bool = False
    stable: bool = False
    message: str = ""


@dataclass(frozen=True)
class ProductPageResult:
    document: str
    source: str
    resolved_url: str
    requests: int
    deployment_id: str = ""


class FastAdaptiveController:
    def __init__(
        self,
        limiter,
        *,
        minimum,
        reduction_factor,
        pause_seconds,
        event_callback=None,
    ):
        self.limiter = limiter
        self.minimum = max(1, int(minimum))
        self.reduction_factor = min(max(float(reduction_factor), 0.10), 0.95)
        self.pause_seconds = max(0.0, float(pause_seconds))
        self.event_callback = event_callback
        self.restrictions = 0
        self.blocked_until = 0.0
        self.last_reduction_at = 0.0
        self._adaptation_lock = asyncio.Lock()

    async def wait_ean_allowed(self):
        while True:
            delay = self.blocked_until - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, 1.0))

    async def report_restriction(self, reason, retry_after=0.0):
        async with self._adaptation_lock:
            self.restrictions += 1
            now = time.monotonic()
            pause = max(self.pause_seconds, max(0.0, float(retry_after)))
            self.blocked_until = max(self.blocked_until, now + pause)

            if now - self.last_reduction_at < ADAPTATION_COOLDOWN_SECONDS:
                return

            old_limit = self.limiter.limit
            reduced = max(self.minimum, int(old_limit * self.reduction_factor))
            if reduced >= old_limit and old_limit > self.minimum:
                reduced = old_limit - 1
            reduced = max(self.minimum, reduced)
            self.last_reduction_at = now

            if reduced != old_limit:
                await self.limiter.set_limit(reduced)

            self._emit(
                BarcodeFetchMetrics(
                    restrictions=self.restrictions,
                    concurrency=reduced,
                    persist_tuning=True,
                    message=(
                        "BAUHAUS barcode concurrency adjusted: "
                        f"{old_limit}->{reduced}; reason={reason}; "
                        f"restrictions={self.restrictions}"
                    ),
                )
            )

    def finish(self):
        self._emit(
            BarcodeFetchMetrics(
                restrictions=self.restrictions,
                concurrency=self.limiter.limit,
                persist_tuning=True,
                stable=True,
            )
        )

    def _emit(self, event):
        if self.event_callback is not None:
            self.event_callback(event)


async def fetch_barcodes(
    targets,
    concurrency,
    *,
    minimum=1,
    result_callback=None,
    stop_event=None,
):
    results = []

    def emit(item):
        if result_callback is None:
            results.append(item)
        else:
            result_callback(item)

    targets_by_key = defaultdict(list)
    ordered_keys = []
    for target in targets:
        if target.key not in targets_by_key:
            ordered_keys.append(target.key)
        targets_by_key[target.key].append(target)

    work_queue = asyncio.Queue()
    for key in ordered_keys:
        work_queue.put_nowait(key)

    limiter = AdjustableLimiter(concurrency)
    controller = FastAdaptiveController(
        limiter,
        minimum=minimum,
        reduction_factor=getattr(
            settings,
            "BAUHAUS_BARCODE_REDUCTION_FACTOR",
            0.70,
        ),
        pause_seconds=getattr(
            settings,
            "BAUHAUS_BARCODE_RESTRICTION_PAUSE_SECONDS",
            5,
        ),
        event_callback=emit,
    )
    resolved_offer_ids = set()
    resolution_lock = asyncio.Lock()
    deployment_id = ""

    async def emit_key(key, ean, source):
        async with resolution_lock:
            unresolved = [
                target
                for target in targets_by_key.get(key, ())
                if target.offer_id not in resolved_offer_ids
            ]
            resolved_offer_ids.update(target.offer_id for target in unresolved)
        for target in unresolved:
            emit(
                BarcodeFetchResult(
                    offer_id=target.offer_id,
                    ean=ean,
                    source=source,
                )
            )
        return len(unresolved)

    async def key_is_resolved(key):
        async with resolution_lock:
            return all(
                target.offer_id in resolved_offer_ids
                for target in targets_by_key.get(key, ())
            )

    async def process_key(key, *, force_html=False):
        nonlocal deployment_id
        if await key_is_resolved(key):
            return

        target = targets_by_key[key][0]
        page = await fetch_product_page(
            session,
            limiter,
            controller,
            target.sku,
            target.product_url,
            deployment_id=deployment_id,
            force_html=force_html,
        )
        if page.deployment_id:
            deployment_id = page.deployment_id

        related_found = 0
        if page.document:
            records = extract_related_product_eans(page.document)
            own_ean = records.get(key, "")
            own_source = f"{page.source}_product_data"
            if not own_ean and page.source == "html":
                own_ean = extract_ean_from_jsonld(
                    page.document,
                    expected_sku=target.sku,
                )
                own_source = "html_jsonld"
            if not own_ean and page.source == "html":
                own_ean = extract_ean_from_next_data(page.document)
                own_source = "html_next_data"

            if own_ean:
                await emit_key(key, own_ean, own_source)

            for related_key, ean in records.items():
                if related_key == key or related_key not in targets_by_key:
                    continue
                related_found += await emit_key(
                    related_key,
                    ean,
                    f"{page.source}_related_product",
                )

            if not await key_is_resolved(key):
                await emit_key(key, "", "ean_not_found")
        else:
            await emit_key(key, "", page.source)

        emit(
            BarcodeFetchMetrics(
                pages=1,
                http_requests=page.requests,
                related_found=related_found,
            )
        )

    async with CurlAsyncSession(
        headers=HEADERS,
        impersonate="chrome",
        max_clients=concurrency,
    ) as session:
        first_key = ordered_keys[0]
        try:
            await process_key(first_key, force_html=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await emit_key(
                first_key,
                "",
                f"worker_error:{type(exc).__name__}",
            )
            emit(BarcodeFetchMetrics(pages=1))

        async def worker():
            while True:
                key = await work_queue.get()
                try:
                    if key == first_key or await key_is_resolved(key):
                        continue
                    try:
                        await process_key(key)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await emit_key(
                            key,
                            "",
                            f"worker_error:{type(exc).__name__}",
                        )
                        emit(BarcodeFetchMetrics(pages=1))
                finally:
                    work_queue.task_done()

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(concurrency, len(ordered_keys)))
        ]
        join_task = asyncio.create_task(work_queue.join())
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

    controller.finish()
    return results


async def fetch_product_page(
    session,
    limiter,
    controller,
    sku,
    product_url,
    *,
    deployment_id="",
    force_html=False,
):
    requests = 0
    if deployment_id and not force_html:
        rsc_url = _add_rsc_token(product_url)
        rsc_headers = {
            **HEADERS,
            "accept": "*/*",
            "referer": product_url,
            "rsc": "1",
            "next-url": "/",
            "x-deployment-id": deployment_id,
        }
        rsc_headers.pop("upgrade-insecure-requests", None)
        document, source, resolved_url, count = await _request_document(
            session,
            limiter,
            controller,
            rsc_url,
            rsc_headers,
            source="rsc",
        )
        requests += count
        if document and _document_has_ean_data(document):
            return ProductPageResult(
                document=document,
                source="rsc",
                resolved_url=resolved_url,
                requests=requests,
                deployment_id=deployment_id,
            )
        if not document and _should_abort_rsc_fallback(source):
            return ProductPageResult(
                document="",
                source=source,
                resolved_url=resolved_url,
                requests=requests,
                deployment_id=deployment_id,
            )

    document, source, resolved_url, count = await _request_document(
        session,
        limiter,
        controller,
        product_url,
        {
            **HEADERS,
            "accept": "text/html,application/xhtml+xml",
            "referer": BASE_URL + "/",
        },
        source="html",
    )
    requests += count
    new_deployment_id = extract_deployment_id(document) if document else ""
    return ProductPageResult(
        document=document,
        source="html" if document else source,
        resolved_url=resolved_url,
        requests=requests,
        deployment_id=new_deployment_id or deployment_id,
    )


async def _request_document(
    session,
    limiter,
    controller,
    url,
    headers,
    *,
    source,
):
    request_count = 0
    resolved_url = url
    last_source = "request_failed"
    for attempt in range(1, EAN_MAX_RETRIES + 1):
        await controller.wait_ean_allowed()
        try:
            async with limiter:
                await asyncio.sleep(random.uniform(0.002, 0.015))
                response = await session.get(
                    url,
                    headers=headers,
                    timeout=EAN_REQUEST_TIMEOUT,
                    allow_redirects=True,
                )
            request_count += 1
            resolved_url = str(response.url)
            status = response.status_code
            if status == 200:
                return response.text, source, resolved_url, request_count

            last_source = f"http_{status}"
            if status in {403, 408, 429}:
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                await controller.report_restriction(
                    f"HTTP {status} on {source}",
                    retry_after=retry_after,
                )
            if status not in {403, 408, 429, 500, 502, 503, 504}:
                return "", last_source, resolved_url, request_count
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            request_count += 1
            last_source = f"request_failed:{type(exc).__name__}"

        if attempt < EAN_MAX_RETRIES:
            await asyncio.sleep(_retry_delay(attempt))

    return "", last_source, resolved_url, request_count


def extract_related_product_eans(document):
    records = {}
    versions = document_versions(document)
    for version in versions:
        for marker in RELATED_DATA_MARKERS:
            for value in json_values_after_marker(version, marker):
                for node in walk_json(value):
                    if isinstance(node, dict):
                        _add_product_record(records, node)

        for match in RELATED_RECORD_PATTERN.finditer(version):
            body = match.group("body")
            ean_match = EAN_FIELD_PATTERN.search(body)
            url_match = URL_FIELD_PATTERN.search(body)
            if not ean_match or not url_match:
                continue
            _add_record_values(
                records,
                _decode_json_string(match.group(1)),
                _decode_json_string(ean_match.group(1)),
                _decode_json_string(url_match.group(1)),
            )
    return records


def _add_product_record(records, node):
    _add_record_values(
        records,
        clean_text(node.get("sku")),
        barcode_from_mapping(node),
        clean_text(
            node.get("canonical_url")
            or node.get("product_url")
            or node.get("url")
        ),
    )


def _add_record_values(records, sku, ean, product_url):
    sku = clean_text(sku)
    ean = validate_ean(ean)
    normalized_url = normalize_product_url(product_url)
    if sku and ean and normalized_url:
        records[(sku, normalized_url)] = ean


def _decode_json_string(value):
    try:
        return json.loads(f'"{value}"')
    except (json.JSONDecodeError, TypeError):
        return clean_text(value)


def normalize_product_url(value):
    text = clean_text(value)
    if not text:
        return ""
    absolute = urljoin(BASE_URL + "/", text)
    parts = urlsplit(absolute)
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def validate_ean(value):
    text = str(value).strip() if value is not None else ""
    if not text.isdigit() or len(text) not in {8, 12, 13, 14}:
        return ""
    return normalize_barcode_candidate(text)


def is_fetch_error(source):
    return source.startswith(("http_", "request_failed", "worker_error"))


def _add_rsc_token(product_url):
    parts = urlsplit(product_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "_rsc"]
    token = "".join(
        random.choice("abcdefghijklmnopqrstuvwxyz0123456789")
        for _ in range(6)
    )
    query.append(("_rsc", token))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _document_has_ean_data(document):
    return "ean" in document.lower() or "gtin" in document.lower()


def _should_abort_rsc_fallback(source):
    return source.startswith(
        ("http_403", "http_408", "http_429", "request_failed")
    )


def _retry_after_seconds(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _retry_delay(attempt):
    return min(30.0, 1.8 ** attempt + random.uniform(0.5, 1.8))


async def _wait_for_stop(stop_event):
    while not stop_event.is_set():
        await asyncio.sleep(0.25)
