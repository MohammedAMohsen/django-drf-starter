"""The project's URL map.

Everything lives under /api/<version>/ except /api/health/ (a monitor should
not have to follow our version numbers) and /api/schema/ (it describes every
version at once).

    /admin/                             the admin (ADMIN_URL moves it)
    /api/v1/auth/users/                 register, me, activate, reset password
    /api/v1/auth/jwt/create/            sign in -> access + refresh
    /api/v1/auth/jwt/refresh/           renew the access token
    /api/v1/auth/users/logout/          blacklist a refresh token
    /api/v1/google/                     sign in with a Google ID token
    /api/v1/profile/                    people search (needs a term)
    /api/v1/profile/me/                 own profile: GET / PUT / PATCH
    /api/v1/profile/<username>/         someone's public profile
    /api/health/                        database + cache probe
    /api/schema/swagger-ui/             the API, documented and clickable

Adding v2: register the new views in a second `api/<version>/` block and add
"v2" to ALLOWED_VERSIONS. Unchanged views serve both versions, and
`request.version` tells a view which one it is answering.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.routers import DefaultRouter

from apps.accounts.views import CustomUserViewSet, ThrottledTokenObtainPairView
from config.health import health

# Djoser's user endpoints, pointed at our subclass so the extra actions
# (logout, change-email, set-new-password) come with them.
router = DefaultRouter()
router.register("users", CustomUserViewSet, basename="user")

# Before djoser's include, so it wins the match — swapped, the rate limit on
# sign-in silently stops applying.
auth_patterns = [
    path("jwt/create/", ThrottledTokenObtainPairView.as_view(), name="jwt-create"),
    path("", include("djoser.urls.jwt")),
    path("", include(router.urls)),
]

# Everything that carries a version number.
api_patterns = [
    path("auth/", include(auth_patterns)),
    path("", include("apps.accounts.urls")),
    # path("", include("apps.<yourapp>.urls")),
]

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),

    # These two come first: `api/<version>/` below would otherwise match
    # "health" as a version number and answer 404.
    path("api/health/", health, name="health"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/schema/swagger-ui/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),

    path("api/<str:version>/", include(api_patterns)),
]

# Uploads. In production Nginx serves /media/ off the disk; `static()`
# returns an empty list when DEBUG is False.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# The front end, if there is one. Last, so every pattern above keeps winning.
# The negative lookahead stops a mistyped API path returning the HTML shell
# with a 200, which the client would try to parse as JSON.
if settings.SERVE_SPA:
    from config.spa import app_shell

    reserved = "|".join(["api/", "media/", "static/", "silk/", settings.ADMIN_URL])
    urlpatterns += [
        re_path(rf"^(?!{reserved}).*$", app_shell, name="app-shell"),
    ]

# The query profiler. Asking INSTALLED_APPS rather than repeating silk's own
# `DEBUG and not TESTING` keeps the two from drifting: they had, and
# `manage.py test --debug-mode` raised RuntimeError here.
if 'silk' in settings.INSTALLED_APPS:
    urlpatterns += [path("silk/", include("silk.urls", namespace="silk"))]
