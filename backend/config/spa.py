"""Serve the built single-page front end from Django.

Only needed when the front end is deployed on the same domain as the API.
Nginx serves the real files and sends every other path here — without which
/activate/<uid>/<token> is a Django 404 and nobody can activate an account
from the mail we just sent.

The file is read once per build, keyed on its mtime.

If shared links need their own title and preview image (WhatsApp, X, Facebook
run no JavaScript), this is where to swap the <head> per page.
"""

import os

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

_cache = {"mtime": None, "html": None}


def _shell():
    path = settings.SPA_INDEX
    try:
        mtime = os.stat(path).st_mtime
    except FileNotFoundError:
        return None
    if _cache["mtime"] != mtime:
        with open(path, encoding="utf-8") as handle:
            _cache.update(mtime=mtime, html=handle.read())
    return _cache["html"]


@require_GET
# "Revalidate before reusing", not "never store": a deploy must not leave a
# visitor on an old index.html pointing at asset names that are gone.
@cache_control(no_cache=True)
def app_shell(request):
    html = _shell()
    if html is None:
        return HttpResponseNotFound(
            "The front end has not been built. Run `npm run build` in frontend/, "
            "or point SPA_INDEX at the built index.html."
        )
    return HttpResponse(html)
