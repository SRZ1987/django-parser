import hashlib
import ipaddress
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse

from .analytics import safely_record_site_visit
from .seo import NOINDEX_PREFIXES


logger = logging.getLogger(__name__)


class PublicReadRateLimitMiddleware:
    SEARCH_PREFIXES = ("/search/", "/products/", "/catalog/")
    DETAIL_PREFIXES = ("/offer/", "/product/ean/")
    SEARCH_ENGINE_MARKERS = (
        "googlebot",
        "google-inspectiontool",
        "bingbot",
        "bingpreview",
        "applebot",
        "duckduckbot",
        "yandexbot",
        "baiduspider",
    )

    def __init__(self, get_response):
        self.get_response = get_response
        self._last_cache_error_log_at = 0

    def __call__(self, request):
        policy = self._policy(request)
        if policy is None:
            return self.get_response(request)

        scope, limit = policy
        retry_after = self._retry_after_if_limited(request, scope, limit)
        if retry_after is None:
            return self.get_response(request)

        client_fingerprint = self._fingerprint(self._client_ip(request))
        logger.warning(
            "Public read rate limit exceeded: scope=%s client=%s path=%s",
            scope,
            client_fingerprint,
            request.path[:200],
        )
        if request.path.startswith("/search/suggestions/"):
            response = JsonResponse(
                {"error": "rate_limited", "retry_after": retry_after},
                status=429,
            )
        else:
            response = HttpResponse(
                "Too many requests. Please try again shortly.",
                status=429,
                content_type="text/plain; charset=utf-8",
            )
        response["Retry-After"] = str(retry_after)
        response["Cache-Control"] = "no-store"
        return response

    def _policy(self, request):
        if not settings.PUBLIC_RATE_LIMIT_ENABLED or request.method != "GET":
            return None

        user_agent = request.META.get("HTTP_USER_AGENT", "").lower()
        if any(marker in user_agent for marker in self.SEARCH_ENGINE_MARKERS):
            return None

        if request.path.startswith("/search/suggestions/"):
            return "suggestions", settings.PUBLIC_RATE_LIMIT_SUGGESTIONS_REQUESTS
        if request.path.startswith(self.SEARCH_PREFIXES):
            return "search", settings.PUBLIC_RATE_LIMIT_SEARCH_REQUESTS
        if request.path.startswith(self.DETAIL_PREFIXES):
            return "detail", settings.PUBLIC_RATE_LIMIT_DETAIL_REQUESTS
        return None

    def _retry_after_if_limited(self, request, scope, limit):
        if limit <= 0:
            return None

        window = max(settings.PUBLIC_RATE_LIMIT_WINDOW_SECONDS, 1)
        now = int(time.time())
        current_window = now // window
        retry_after = window - (now % window)
        client_id = self._client_id(request)
        ip_address = self._client_ip(request)

        try:
            client_count = self._increment(scope, "client", client_id, current_window, window)
            ip_count = self._increment(scope, "ip", ip_address, current_window, window)
        except Exception:
            monotonic_now = time.monotonic()
            if monotonic_now - self._last_cache_error_log_at >= 60:
                logger.exception("Public read rate limit cache is unavailable")
                self._last_cache_error_log_at = monotonic_now
            return None

        ip_limit = limit * max(settings.PUBLIC_RATE_LIMIT_IP_MULTIPLIER, 1)
        if client_count > limit or ip_count > ip_limit:
            return max(retry_after, 1)
        return None

    @staticmethod
    def _increment(scope, identity_type, identity, current_window, timeout):
        identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        key = f"public-read:{scope}:{identity_type}:{identity_hash}:{current_window}"
        if cache.add(key, 1, timeout=timeout + 1):
            return 1
        return cache.incr(key)

    def _client_id(self, request):
        if request.user.is_authenticated:
            return f"user:{request.user.pk}"
        visitor_id = request.session.get("_analytics_visitor_id")
        if visitor_id:
            return f"visitor:{visitor_id}"
        return f"ip:{self._client_ip(request)}"

    @staticmethod
    def _client_ip(request):
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
        candidates = [item.strip() for item in forwarded_for.split(",") if item.strip()]
        candidates.append(request.META.get("REMOTE_ADDR", ""))
        for candidate in candidates:
            try:
                return ipaddress.ip_address(candidate).compressed
            except ValueError:
                continue
        return "unknown"

    @staticmethod
    def _fingerprint(value):
        salted_value = f"{settings.SECRET_KEY}:{value}".encode("utf-8")
        return hashlib.sha256(salted_value).hexdigest()[:12]


class SeoHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(NOINDEX_PREFIXES):
            response["X-Robots-Tag"] = "noindex, nofollow"
        return response


class AnalyticsMiddleware:
    EXCLUDED_PREFIXES = (
        "/admin/",
        "/statistics/",
        "/static/",
        "/media/",
        "/out/",
        "/price-comparisons/",
    )
    BOT_USER_AGENT_MARKERS = (
        "bot",
        "crawler",
        "spider",
        "slurp",
        "bingpreview",
        "facebookexternalhit",
        "whatsapp",
        "telegrambot",
        "healthcheck",
        "uptimerobot",
        "curl/",
        "wget/",
        "python-requests",
        "aiohttp",
        "go-http-client",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        content_type = response.get("Content-Type", "")
        user_agent = request.META.get("HTTP_USER_AGENT", "").strip().lower()
        accept = request.META.get("HTTP_ACCEPT", "").lower()
        fetch_destination = request.META.get("HTTP_SEC_FETCH_DEST", "").lower()
        fetch_mode = request.META.get("HTTP_SEC_FETCH_MODE", "").lower()
        if (
            request.method == "GET"
            and response.status_code < 400
            and content_type.startswith("text/html")
            and "text/html" in accept
            and fetch_destination in ("", "document")
            and fetch_mode in ("", "navigate")
            and not request.path.startswith(self.EXCLUDED_PREFIXES)
            and user_agent
            and not any(marker in user_agent for marker in self.BOT_USER_AGENT_MARKERS)
        ):
            safely_record_site_visit(request)
        return response
