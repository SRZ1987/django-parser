from .analytics import safely_record_site_visit
from .seo import NOINDEX_PREFIXES


class SeoHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(NOINDEX_PREFIXES):
            response["X-Robots-Tag"] = "noindex, nofollow"
        return response


class AnalyticsMiddleware:
    EXCLUDED_PREFIXES = ("/admin/", "/statistics/", "/static/", "/media/", "/out/")
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
        if (
            request.method == "GET"
            and response.status_code < 400
            and content_type.startswith("text/html")
            and not request.path.startswith(self.EXCLUDED_PREFIXES)
            and user_agent
            and not any(marker in user_agent for marker in self.BOT_USER_AGENT_MARKERS)
        ):
            safely_record_site_visit(request)
        return response
