"""One shape for every error the API returns.

Keeps DRF's status codes and its per-field validation bodies, and adds:

  * a machine-readable `code` on every flat error, so clients branch on a
    constant instead of matching English text
  * Django's own exceptions translated to the right status instead of 500
  * anything unexpected logged with its traceback and answered flatly, since
    a stack trace in a response body names the file layout and often the SQL
"""

import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied, RequestDataTooBig, SuspiciousOperation
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


def api_exception_handler(exc, context):
    # Translate Django's exceptions into DRF's, so the rest deals with one
    # family.
    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, PermissionDenied):
        exc = exceptions.PermissionDenied()
    elif isinstance(exc, RequestDataTooBig):
        # DATA_UPLOAD_MAX_MEMORY_SIZE exceeded. Django handles this itself
        # only for its own handler; DRF reads the body inside the view, so it
        # landed in the 500 branch.
        return Response(
            {"detail": "Request body too large.", "code": "request_too_large"},
            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    elif isinstance(exc, SuspiciousOperation):
        # A bad host header, a broken multipart boundary: client errors, not
        # worth a 500 or a Sentry report.
        return Response(
            {"detail": "Malformed request.", "code": "suspicious_operation"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    response = drf_exception_handler(exc, context)

    if response is None:
        # Nothing recognised it: a real bug.
        view = context.get("view").__class__.__name__ if context.get("view") else "?"
        request = context.get("request")
        logger.error(
            "Unhandled %s in %s (%s %s)",
            exc.__class__.__name__,
            view,
            getattr(request, "method", "?"),
            getattr(request, "path", "?"),
            exc_info=True,
        )
        # In development, hand it back to Django so its debug page shows the
        # traceback and the query log, instead of one flat sentence.
        if settings.DEBUG:
            raise exc
        return Response(
            {"detail": "A server error occurred.", "code": "server_error"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # A validation error's body is a dict of field -> messages and must stay
    # that way; `code` is only added to the flat {"detail": ...} form.
    if isinstance(response.data, dict) and "detail" in response.data:
        response.data.setdefault("code", getattr(exc, "default_code", "error"))

    # DRF puts the wait only in the English detail string.
    if isinstance(exc, exceptions.Throttled) and exc.wait:
        response["Retry-After"] = str(int(exc.wait))

    return response
