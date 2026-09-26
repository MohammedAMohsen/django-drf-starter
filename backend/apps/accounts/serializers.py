"""Everything that crosses the wire, in and out of the accounts app.

The private/public profile split is worth copying into any new app: one
serializer with conditional fields is how a private field ends up in a
public response, and two serializers cannot make that mistake.
"""

import binascii

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from djoser.serializers import (
    SendEmailResetSerializer,
    UidAndTokenSerializer,
    UserCreatePasswordRetypeSerializer,
    UserSerializer,
)
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Profile, User
from .services import create_user_with_unique_username
from .tokens import activation_token, email_change_token
from .validators import (
    validate_avatar as shrink_avatar,
)
from .validators import (
    validate_avatar_cover,
    validate_email,
    validate_name,
    validate_username,
)


class CustomUserCreateSerializer(UserCreatePasswordRetypeSerializer):
    """Signup. "Retype" means `re_password` is required too.

    No username field: it is derived from the email in perform_create, and
    can be changed later from the settings page.
    """

    class Meta(UserCreatePasswordRetypeSerializer.Meta):
        model = User
        fields = ("first_name", "last_name", "email", "password")

    def validate_first_name(self, value):
        return validate_name(value)

    def validate_last_name(self, value):
        return validate_name(value)

    def validate_email(self, value):
        value = validate_email(value)
        # DRF's own uniqueness check compares the address as typed, so
        # `BOSS@Example.com` missed the stored `boss@example.com` and the
        # database refused it with an error naming no field.
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with that email already exists.")
        return value

    def perform_create(self, validated_data):
        """Create the user through the same service the Google path uses.

        `create_user`, not `create` — that is what hashes the password.
        `is_active = False` is djoser's rule, repeated because this method
        replaces its version rather than calling it.
        """
        from djoser.conf import settings as djoser_settings

        with transaction.atomic():
            user = create_user_with_unique_username(
                create=User.objects.create_user, **validated_data
            )
            if djoser_settings.SEND_ACTIVATION_EMAIL:
                user.is_active = False
                user.save(update_fields=["is_active"])
        return user


class CustomUserSerializer(UserSerializer):
    """What /api/v1/auth/users/me/ returns — the account, not the profile.

    The avatar comes across from the profile: every "who am I" view needs
    it, and a second request for one field is a second round trip.
    """

    full_name = serializers.CharField(source="get_full_name", read_only=True)
    # A method field, not ImageField(source="profile.avatar"): that raises
    # when the profile is missing, and this endpoint is where such a user
    # lands. See views.py::me.
    avatar = serializers.SerializerMethodField()
    has_usable_password = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        model = User
        fields = (
            "id", "first_name", "last_name", "full_name", "avatar",
            "username", "email", "date_joined", "has_usable_password",
        )
        extra_kwargs = {
            "date_joined": {"read_only": True},
            "email": {"read_only": True},  # changed through change-email only
        }

    @extend_schema_field(serializers.ImageField)
    def get_avatar(self, obj):
        profile = getattr(obj, "profile", None)
        if profile is None or not profile.avatar:
            return None
        url = profile.avatar.url
        request = self.context.get("request")
        # Absolute, so the client need not know where media is served from.
        return request.build_absolute_uri(url) if request else url

    def get_has_usable_password(self, obj) -> bool:
        # False for a Google account that never set one, so the interface can
        # offer "set a password" instead of asking for a current one.
        return obj.has_usable_password()

    def validate_first_name(self, value):
        return validate_name(value)

    def validate_last_name(self, value):
        return validate_name(value)

    def validate_username(self, value):
        return validate_username(value)


class ActivationSerializer(UidAndTokenSerializer):
    """Checks the activation link with the activation generator.

    Djoser validates with the default generator, the same one it uses for
    password reset, so an activation link doubled as a reset link. Ours has
    its own salt — see tokens.py.
    """

    default_error_messages = {
        "invalid_token": "Invalid or expired activation link.",
        "stale_token": "This account is already activated.",
    }

    def validate(self, attrs):
        # Skips UidAndTokenSerializer's own token check; the uid lookup is
        # redone below.
        attrs = super(UidAndTokenSerializer, self).validate(attrs)
        self.user = self._user_from_uid(attrs)
        if not activation_token.check_token(self.user, attrs["token"]):
            self.fail("invalid_token")

        # Activation sets is_active, which is what enforces a ban: without
        # this a banned person only had to ask for a fresh link.
        if self.user.is_banned:
            self.fail("invalid_token")

        # Djoser refuses a link for an already-active account, and replacing
        # its validate() dropped that check — a single-use link matters, and
        # any signal hung off activation would fire once per click.
        if self.user.is_active:
            self.fail("stale_token")
        return attrs

    def _user_from_uid(self, attrs):
        import binascii

        try:
            uid = force_str(urlsafe_base64_decode(attrs["uid"]))
            return User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError, DjangoValidationError, binascii.Error):
            self.fail("invalid_token")


class LowercasedEmailSerializer(SendEmailResetSerializer):
    """Djoser's "email me a link" form, with the address lowercased first.

    The lookup is an exact match and addresses are stored lowercase, so
    `Mixed@Example.com` found nobody — silently, because the endpoint answers
    204 either way. Used for password reset and resend-activation.
    """

    def validate_email(self, value):
        return value.lower()

    def get_user(self, is_active=True):
        """No link of any kind goes to a banned account.

        A resend-activation link sets is_active, the flag holding the ban.
        None makes the endpoint answer as it would for an unknown address.
        """
        user = super().get_user(is_active=is_active)
        if user is not None and user.is_banned:
            return None
        return user


class ChangeEmailSerializer(serializers.Serializer):
    """Step one of a change of address: validate, then park it.

    `current_password` is required because moving the address is how an
    account is taken permanently — a stolen access token must not be enough.
    """

    email = serializers.EmailField()
    current_password = serializers.CharField(write_only=True)

    default_error_messages = {
        "invalid_password": "Your current password is not correct.",
    }

    def validate_current_password(self, value):
        user = self.context["request"].user
        # A Google account has no password to prove, and must set one first.
        if not user.has_usable_password() or not user.check_password(value):
            self.fail("invalid_password")
        return value

    def validate_email(self, value):
        value = validate_email(value)
        user = self.context["request"].user
        if value == user.email:
            raise serializers.ValidationError("This is already your current email.")
        # Only a *taken* address is refused, not one merely pending for
        # somebody else — that let anyone reserve an address for three days.
        # confirm_user_email checks again at the last step.
        if User.objects.filter(email=value).exclude(pk=user.pk).exists():
            raise serializers.ValidationError("This email is already in use.")
        return value.lower()


class ConfirmEmailChangeSerializer(serializers.Serializer):
    """Step two: the link from the mail comes back here."""

    uid = serializers.CharField()
    token = serializers.CharField()

    default_error_messages = {
        "invalid_link": "Invalid or expired confirmation link.",
        "no_pending_email": "No pending email change request found.",
        "user_not_found": "Invalid confirmation link.",
    }

    def validate(self, attrs):
        # A truncated link raises binascii.Error and a non-numeric uid raises
        # ValueError; uncaught, a bad link would be a 500.
        try:
            uid = force_str(urlsafe_base64_decode(attrs["uid"]))
            user = User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError, DjangoValidationError, binascii.Error):
            self.fail("user_not_found")
        # The email-change generator, not the default one — see tokens.py.
        if not email_change_token.check_token(user, attrs["token"]):
            self.fail("invalid_link")
        if not user.pending_email:
            self.fail("no_pending_email")
        attrs["user"] = user
        return attrs


class LogoutSerializer(serializers.Serializer):
    """Signing out means blacklisting the refresh token.

    An access token cannot be revoked, so it stays valid until it expires —
    which is why its lifetime is short. Blacklisting the refresh token is
    what stops the session renewing.
    """

    refresh = serializers.CharField()

    def save(self, **kwargs):
        try:
            token = RefreshToken(self.validated_data["refresh"])
        except Exception:
            raise serializers.ValidationError("Invalid token") from None

        # The token must belong to whoever is asking, or one account could
        # end another's session. Compared as strings because SimpleJWT stores
        # the claim as str(pk): "7" != 7 would always be true.
        user = self.context["request"].user
        if str(token.get("user_id")) != str(user.pk):
            raise serializers.ValidationError("Invalid token")

        token.blacklist()


class PrivateProfileSerializer(serializers.ModelSerializer):
    """The owner's own profile: everything, and writable."""

    # Removing a picture differs from not sending one, and an ImageField
    # cannot say so: null is rejected and multipart cannot carry it.
    clear_avatar = serializers.BooleanField(write_only=True, required=False, default=False)
    clear_cover = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Profile
        fields = (
            "avatar", "cover", "bio", "birth_date", "location",
            "website", "github", "linkedin", "is_identity_verified",
            "clear_avatar", "clear_cover",
        )
        extra_kwargs = {
            # A badge the owner must not award themselves.
            "is_identity_verified": {"read_only": True},
        }

    def validate_avatar(self, value):
        return shrink_avatar(value)

    def validate_cover(self, value):
        return validate_avatar_cover(value)

    def update(self, instance, validated_data):
        """Clearing means back to the default picture, not to nothing.

        One rule holds: a profile always has a picture, so no component needs
        a fallback. django-cleanup deletes the replaced upload and refuses to
        delete a field's default, so the shared default survives.
        """
        for field, flag in (("avatar", "clear_avatar"), ("cover", "clear_cover")):
            if validated_data.pop(flag, False) and field not in validated_data:
                validated_data[field] = Profile._meta.get_field(field).default
        return super().update(instance, validated_data)


class PublicProfileSerializer(serializers.ModelSerializer):
    """What anyone may see. Missing against the private one: birth_date."""

    username = serializers.CharField(source="user.username", read_only=True)
    full_name = serializers.CharField(source="user.get_full_name", read_only=True)

    class Meta:
        model = Profile
        fields = (
            "username", "full_name", "avatar", "cover", "bio",
            "website", "github", "linkedin", "is_identity_verified",
        )


class GoogleAuthSerializer(serializers.Serializer):
    token = serializers.CharField()


class SetNewPasswordSerializer(serializers.Serializer):
    """For an account that has no password yet (a Google signup).

    No current_password, because there is none — which is why the view checks
    has_usable_password() first, or this would be a reset with no proof.
    """

    # No min_length: validate_password below carries the real policy.
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value):
        validate_password(value, user=self.context["request"].user)
        return value


class AccountDisabled(APIException):
    """The credentials were right; an administrator closed the account.

    Kept apart from AccountInactive, which would point a banned person at
    resend-activation — the one door they must not be shown.
    """

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "This account has been disabled. Contact support."
    default_code = "account_disabled"

    def __init__(self):
        super().__init__({"detail": self.default_detail, "code": self.default_code})


class AccountInactive(APIException):
    """The credentials were right; the account is not activated.

    403, not 401: 401 means "I do not know who you are", and by now we do.
    The `code` is in the body because the interface branches on it.
    """

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "This account has not been activated yet."
    default_code = "account_inactive"

    def __init__(self):
        super().__init__({"detail": self.default_detail, "code": self.default_code})


class LoginSerializer(TokenObtainPairSerializer):
    """Sign-in, with one failure separated from the rest.

    By default an unknown email, a wrong password and a correct password on
    an unactivated account all give the same 401, and the third leaves a real
    owner with no way forward. Saying so is not enumeration: whoever reaches
    that branch has just supplied the right password, and the endpoint allows
    ten attempts a minute. AllowAllUsersModelBackend is what returns the user
    even when inactive, so no second password check is needed.
    """

    def validate(self, attrs):
        # Addresses are stored lowercase and the lookup is case-sensitive, so
        # `Mixed@Example.com` with the right password read as "wrong".
        if self.username_field in attrs:
            attrs[self.username_field] = attrs[self.username_field].lower()
        try:
            return super().validate(attrs)
        except AuthenticationFailed:
            user = getattr(self, "user", None)
            if user is not None and not user.is_active:
                # Both are is_active=False and must not read the same.
                if user.is_banned:
                    raise AccountDisabled() from None
                raise AccountInactive() from None
            raise
