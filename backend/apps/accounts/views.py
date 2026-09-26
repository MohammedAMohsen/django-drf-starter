"""The account endpoints.

  * CustomUserViewSet — djoser's user endpoints, extended.
  * ProfileViewSet — own profile at /me/, anyone else's at /<username>/,
    and a people search on the list route.
  * ThrottledTokenObtainPairView / GoogleAuthAPIView — the two ways in.

A view decides who and what; how belongs in services.py.
"""

from django.shortcuts import get_object_or_404
from djoser.views import UserViewSet
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import filters, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from .google_auth import get_or_create_user_from_google, verify_google_token
from .models import Profile
from .serializers import (
    ChangeEmailSerializer,
    ConfirmEmailChangeSerializer,
    GoogleAuthSerializer,
    LoginSerializer,
    LogoutSerializer,
    PrivateProfileSerializer,
    PublicProfileSerializer,
    SetNewPasswordSerializer,
)
from .services import change_user_email, confirm_user_email


class CustomUserViewSet(UserViewSet):
    """Djoser's user endpoints plus four of our own, under /api/v1/auth/.

    Inherited, and worth knowing about because they exist whether or not
    anyone writes a line for them:

        POST   users/                      register
        POST   users/activation/           activate from the mailed link
        POST   users/resend_activation/
        POST   users/reset_password/       ask for a reset link
        POST   users/reset_password_confirm/
        POST   users/set_password/         change (needs current_password)
        GET    users/me/                   the signed-in account
        DELETE users/me/                   delete it (needs current_password)

    One consequence of that last line is deliberate and must not be
    "fixed": an account created through Google has no password, so deleting
    it fails until one is set. The front end relies on this — pressing
    Delete sends the person to set-new-password first, then back to delete.
    Do not loosen the password requirement on deletion to work around it.

    The **kwargs on every custom action below is required: the API version
    arrives as a URL keyword argument.
    """

    # With USERNAME_FIELD = email, djoser's set_username / reset_username
    # become a second way to change the address that applies it immediately,
    # with no proof the new address is real — the exact hole the change-email
    # flow closes. None leaves the router nothing to register.
    set_username = None
    reset_username = None
    reset_username_confirm = None

    # These send mail, so they get the tight `auth` budget: on the general
    # rate a stranger could fire 120 messages a minute from our own relay,
    # which is how a sending domain gets blacklisted.
    MAIL_SENDING_ACTIONS = frozenset({
        "create",
        "resend_activation",
        "reset_password",
        "change_email",
    })

    def get_throttles(self):
        if self.action in self.MAIL_SENDING_ACTIONS:
            self.throttle_scope = "auth"
            return [ScopedRateThrottle()]
        return super().get_throttles()

    @action(detail=False, methods=["POST"], url_path="change-email")
    def change_email(self, request, **kwargs):
        """Ask to change address. Nothing changes until the link is opened."""
        serializer = ChangeEmailSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        change_user_email(request.user, serializer.validated_data["email"])
        return Response({"detail": "A verification email will be sent to your new address."})

    @action(
        detail=False,
        methods=["POST"],
        # Deliberately open: whoever opens this link from their mail is not
        # signed in, and often not on the device that asked.
        authentication_classes=[],
        permission_classes=[],
        url_path="confirm-email-change",
    )
    def confirm_email_change(self, request, **kwargs):
        serializer = ConfirmEmailChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        confirm_user_email(serializer.validated_data["user"])
        return Response({"detail": "Email changed successfully. Please log in again."})

    @action(
        detail=False,
        methods=["POST"],
        url_path="set-new-password",
        url_name="set-new-password",
    )
    def set_new_password(self, request, **kwargs):
        """Give a password to an account that has none (a Google signup).

        The guard is this endpoint's whole security: with a password set,
        changing it must go through set_password, which asks for the current
        one. Without it a stolen token could lock the owner out.
        """
        if request.user.has_usable_password():
            return Response(
                {"detail": "Password is already set. Use change password instead.",
                 "code": "password_already_set"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = SetNewPasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        return Response({"detail": "Password has been set successfully."})

    @action(detail=False, methods=["POST"], url_path="logout")
    def logout(self, request, **kwargs):
        serializer = LogoutSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # 205 — "your state is stale, reset it".
        return Response(
            {"detail": "Successfully logged out."}, status=status.HTTP_205_RESET_CONTENT
        )


# The lookup is `username`, which lives on User and not on Profile, so
# spectacular cannot infer its type from the model.
@extend_schema_view(
    retrieve=extend_schema(
        parameters=[
            OpenApiParameter("username", str, OpenApiParameter.PATH, description="Public handle.")
        ]
    )
)
class ProfileViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only for everyone else; writable only through /me/.

    ReadOnlyModelViewSet rather than ModelViewSet on purpose — there is no
    route by which one person can PATCH another's profile, because no such
    route is generated.
    """

    # select_related: every row's serializer reads user.username, which is
    # one extra query per profile without the join.
    queryset = Profile.objects.select_related("user")
    serializer_class = PublicProfileSerializer
    lookup_field = "username"

    filter_backends = [filters.SearchFilter]
    # `^` is "starts with", far cheaper than the default icontains, which
    # can never use an index. It generates UPPER(col) LIKE UPPER('x%'), so a
    # plain index does not serve it either — once the table is large add
    # models.Index(Upper("username"), name="user_username_upper_idx").
    search_fields = ["^user__username", "^user__first_name", "^user__last_name"]

    # The list is a people search, not a directory. A public endpoint that
    # pages through every account is an enumeration tool, not a feature.
    SEARCH_MIN_LENGTH = 2

    def get_throttles(self):
        # The minimum term length raises the cost of enumeration, this caps
        # the rate; neither alone stops a patient crawl.
        if self.action == "list":
            self.throttle_scope = "search"
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            # A deactivated account disappears from search and from every
            # public profile page, in one place.
            .filter(user__is_active=True)
            # Paging needs a deterministic order, or a row lands on two pages.
            .order_by("user__username")
        )

    def get_object(self):
        return get_object_or_404(self.get_queryset(), user__username=self.kwargs["username"])

    def get_permissions(self):
        # /me/ is the only route here that needs an account; opening the rest
        # up is a deliberate line, because IsAuthenticated is the default.
        if self.action == "me":
            return [IsAuthenticated()]
        return [AllowAny()]

    def list(self, request, *args, **kwargs):
        term = request.query_params.get("search", "").strip()
        if len(term) < self.SEARCH_MIN_LENGTH:
            # The same shape a real page has, so the client needs no special
            # case for "no search term".
            return Response({"count": 0, "next": None, "previous": None, "results": []})
        return super().list(request, *args, **kwargs)

    @action(detail=False, methods=["GET", "PUT", "PATCH"], url_path="me")
    def me(self, request, **kwargs):
        # get_or_create, not get: signals.py fires on save() only — not on
        # bulk_create, loaddata, queryset.update() or a raw INSERT. A user
        # row that arrived any of those ways has no profile, and `get` would
        # answer every request from that account with a 500.
        profile, _ = Profile.objects.get_or_create(user=request.user)

        # Without the request, DRF's ImageField returns /media/... instead of
        # the absolute URL the public profile returns for the same picture.
        context = {"request": request}

        if request.method == "GET":
            return Response(PrivateProfileSerializer(profile, context=context).data)

        serializer = PrivateProfileSerializer(
            profile,
            data=request.data,
            partial=request.method == "PATCH",
            context=context,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ThrottledTokenObtainPairView(TokenObtainPairView):
    """Sign in. The one endpoint where guessing pays, so it gets the tight
    `auth` scope (10/min) instead of the anonymous rate (120/min)."""

    throttle_scope = "auth"
    serializer_class = LoginSerializer


# A plain APIView has no serializer for spectacular to read, so without this
# the endpoint is silently missing from the documentation.
@extend_schema(
    request=GoogleAuthSerializer,
    responses=OpenApiResponse(
        response=inline_serializer(
            name="TokenPair",
            fields={"access": serializers.CharField(), "refresh": serializers.CharField()},
        ),
        description="A JWT pair for the signed-in account.",
    ),
)
class GoogleAuthAPIView(APIView):
    """Exchange a Google ID token for our own pair of JWTs."""

    permission_classes = [AllowAny]
    # Same reasoning as sign-in: a Google token is a credential.
    throttle_scope = "auth"
    throttle_classes = [ScopedRateThrottle]

    def post(self, request, **kwargs):
        serializer = GoogleAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = verify_google_token(serializer.validated_data["token"])
        user = get_or_create_user_from_google(payload)
        if not user.is_active:
            # A banned account must not get in through a side door. The
            # wording is SimpleJWT's own, so this path gives away no more.
            raise AuthenticationFailed("No active account found with the given credentials")
        refresh = RefreshToken.for_user(user)
        return Response({"refresh": str(refresh), "access": str(refresh.access_token)})
