"""Project-wide middleware."""

from django.conf import settings

# Not hardcoded, because ADMIN_URL moves the admin.
NOINDEX_PREFIXES = ("/api/", f"/{settings.ADMIN_URL}")


class NoIndexApiMiddleware:
    """Keep the API out of search results without keeping crawlers out of it.

    `Disallow: /api/` in robots.txt is a trap for a site drawn by JavaScript:
    Google renders the page, the page fetches /api/, robots.txt forbids it,
    and the empty state gets indexed. Crawlers must be able to fetch the API;
    they must only not list its JSON as pages.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(NOINDEX_PREFIXES):
            response["X-Robots-Tag"] = "noindex, nofollow"
        return response
