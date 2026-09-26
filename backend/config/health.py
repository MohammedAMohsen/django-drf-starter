"""GET /api/health/ — is this process able to serve?

"gunicorn is running" is not "the site works": the database can refuse
connections while Python answers happily. This checks both, so a monitor (or
deploy.sh after a restart) learns the truth in one request.

Deliberately mute — no version, no hostname — which is why it is safe to
leave open, and it must be open: a monitor carries no token.
"""

import logging

from django.core.cache import cache
from django.db import connection
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

logger = logging.getLogger(__name__)


class HealthThrottle(AnonRateThrottle):
    """Generous, but not unlimited.

    Every call does real work — a database round trip and a cache write — so
    unthrottled this is a free amplifier. Sixty a minute is twice what a
    30-second monitor needs.
    """

    scope = "health"
    rate = "60/min"


def _database():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


def _cache():
    cache.set("health-check", "1", 5)
    if cache.get("health-check") != "1":
        raise RuntimeError("the cache accepted a write but did not return it")


@extend_schema(
    responses=OpenApiResponse(
        response=inline_serializer(
            name="Health",
            fields={
                "status": serializers.CharField(),
                "database": serializers.CharField(),
                "cache": serializers.CharField(),
            },
        ),
        description='200 with status "ok", or 503 with "degraded".',
    ),
)
@api_view(["GET"])
@authentication_classes([])
@permission_classes([])
@throttle_classes([HealthThrottle])
def health(request):
    checks = {}
    for name, probe in (("database", _database), ("cache", _cache)):
        try:
            probe()
            checks[name] = "ok"
        except Exception:
            # The reason goes to the log, not the response: a connection
            # error names the host, the port and often the user.
            logger.error("Health check failed for %s", name, exc_info=True)
            checks[name] = "error"

    healthy = all(value == "ok" for value in checks.values())
    return Response(
        {"status": "ok" if healthy else "degraded", **checks},
        status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
