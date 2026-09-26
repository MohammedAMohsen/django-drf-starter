"""Sign in with Google.

The browser gets an ID token from Google and posts it here. verify_oauth2_token
fetches Google's public keys and checks the signature, expiry, issuer and that
`aud` is our client ID — without that last check anyone could sign in with a
token their own Google app issued. email_verified is the second gate.
"""

from django.conf import settings
from django.db import transaction
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import serializers

from .models import User
from .services import create_user_with_unique_username

# google-auth defaults this network call to 120 seconds — two minutes of a
# gunicorn worker held on one sign-in.
GOOGLE_CERTS_TIMEOUT = 10


class _TimeoutRequest(google_requests.Request):
    """google-auth's transport, with our timeout instead of its default."""

    def __call__(self, url, method="GET", body=None, headers=None,
                 timeout=GOOGLE_CERTS_TIMEOUT, **kwargs):
        return super().__call__(url, method, body, headers, timeout=timeout, **kwargs)


def verify_google_token(token):
    if not settings.GOOGLE_CLIENT_ID:
        raise serializers.ValidationError("Google sign-in is not configured on this server.")
    try:
        payload = id_token.verify_oauth2_token(
            token,
            _TimeoutRequest(),
            settings.GOOGLE_CLIENT_ID,
        )
    except ValueError:
        # Bad signature, expired, wrong audience — one answer for all of
        # them; saying which would tell an attacker what to fix.
        raise serializers.ValidationError("Invalid or expired Google token.") from None

    if not payload.get("email_verified"):
        raise serializers.ValidationError("This Google email is not verified.")

    return payload


def get_or_create_user_from_google(payload):
    """An existing account signs in; a new address becomes an account.

    is_active=True with no activation mail: Google has already proved the
    person controls the address, which is all activation checks.

    set_unusable_password() means no password at all, not a blank one. The
    owner can add one through set-new-password, and has_usable_password on
    /auth/users/me/ tells the interface to offer that.
    """
    # Google can return mixed case; without this the same person would get a
    # second, duplicate account.
    email = payload["email"].lower()
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        # One transaction: a failure between the insert and the password
        # write leaves a password of '', which Django reads as *usable* — so
        # the owner is refused set-new-password and can never set one.
        with transaction.atomic():
            user = create_user_with_unique_username(
                email,
                first_name=payload.get("given_name", ""),
                last_name=payload.get("family_name", ""),
                is_active=True,
            )
            user.set_unusable_password()
            user.save(update_fields=["password"])
        return user

    if user.is_banned:
        return user

    # Signed up the ordinary way, never opened the activation mail, now
    # arriving through Google. `last_login is None` narrows this to an account
    # never used; one signed into before was switched off deliberately.
    if not user.is_active and user.last_login is None:
        # The password goes, and this is the part that matters: an attacker
        # can register with somebody else's address and their own password,
        # and the account sits inactive because the mail went to the victim.
        # Keeping that password would hand the attacker a working key the
        # moment the victim signed in with Google.
        user.is_active = True
        user.set_unusable_password()
        user.save(update_fields=["is_active", "password"])
    return user
