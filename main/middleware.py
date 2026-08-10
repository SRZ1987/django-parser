from .analytics import safely_record_site_visit


class AnalyticsMiddleware:
    EXCLUDED_PREFIXES = ("/admin/", "/statistics/", "/static/", "/media/", "/out/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        content_type = response.get("Content-Type", "")
        if (
            request.method == "GET"
            and response.status_code < 400
            and content_type.startswith("text/html")
            and not request.path.startswith(self.EXCLUDED_PREFIXES)
        ):
            safely_record_site_visit(request)
        return response
