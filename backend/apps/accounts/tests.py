"""Tests for the account flows.

Not here for coverage: each class pins one decision whose breakage would be
silent — the kind that still answers 200 while being wrong. Run them first
thing after copying this project (`python manage.py test`).
"""

import re
import unittest
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone
from PIL import Image
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Profile
from apps.accounts.validators.image import shrink

from .services import username_candidates

User = get_user_model()


class UsernameGenerationTests(TestCase):
    """The username is derived from the email, and must always be usable."""

    def test_a_non_ascii_local_part_still_yields_a_username(self):
        first = next(username_candidates("محمد@example.com"))
        self.assertTrue(first)
        self.assertNotEqual(first, "")

    def test_an_unslugifiable_local_part_falls_back(self):
        self.assertEqual(next(username_candidates("...@example.com")), "user")

    def test_candidates_are_all_distinct(self):
        candidates = list(username_candidates("mohammed@example.com"))
        self.assertEqual(len(candidates), len(set(candidates)))
        self.assertEqual(candidates[0], "mohammed")


class SignupUsesTheSharedUserServiceTests(TestCase):
    """Signup and Google sign-in create users through one function.

    Unifying them is only safe if the password is still hashed, which is what
    the first test here guards.
    """

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def signup(self, email, password="VeryStrongPass!99"):
        return self.client.post(
            "/api/v1/auth/users/",
            {
                "first_name": "Noor",
                "last_name": "Mohsen",
                "email": email,
                "password": password,
                "re_password": password,
            },
            format="json",
        )

    def test_the_password_is_hashed_and_the_account_can_sign_in(self):
        """`create` stores the string as given; `create_user` hashes it.

        The wrong one would write every password in clear text, and nothing
        else would notice: the row exists and the response is 201.
        """
        self.assertEqual(self.signup("hashed@example.com").status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email="hashed@example.com")

        self.assertNotEqual(user.password, "VeryStrongPass!99")
        self.assertTrue(user.has_usable_password())
        self.assertTrue(user.check_password("VeryStrongPass!99"))

        user.is_active = True
        user.save(update_fields=["is_active"])
        response = self.client.post(
            "/api/v1/auth/jwt/create/",
            {"email": "hashed@example.com", "password": "VeryStrongPass!99"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_activation_is_still_required(self):
        self.signup("inactive-by-default@example.com")
        user = User.objects.get(email="inactive-by-default@example.com")
        self.assertFalse(user.is_active)

    def test_a_taken_username_falls_through_to_the_next_candidate(self):
        self.signup("noor@example.com")
        self.signup("noor@other.example.com")
        usernames = set(User.objects.values_list("username", flat=True))
        self.assertIn("noor", usernames)
        self.assertIn("noor1", usernames)

    def test_a_username_collision_mid_insert_is_retried_not_a_500(self):
        """The race itself, forced on the service that owns the retry.

        `exists()` alternates False/True: free before the insert, taken after
        the IntegrityError — the sequence a signup a millisecond earlier
        produces. Driven through the service, not HTTP, so an extra query in
        a serializer does not break it.
        """
        from itertools import cycle
        from unittest.mock import patch

        from apps.accounts.services import create_user_with_unique_username

        User.objects.create_user(
            username="clash", email="taken@other.example.com", password="StrongPass1234"
        )

        with patch.object(
            type(User.objects.all()), "exists", side_effect=cycle([False, True])
        ):
            created = create_user_with_unique_username(
                "clash@example.com",
                create=User.objects.create_user,
                password="VeryStrongPass!99",
            )

        # It took the next candidate rather than raising.
        self.assertNotEqual(created.username, "clash")
        self.assertTrue(created.username.startswith("clash"))
        self.assertTrue(created.check_password("VeryStrongPass!99"))


class ProfileIsCreatedWithTheUserTests(TestCase):
    """The rule everything else assumes: a user always has a profile."""

    def test_creating_a_user_creates_a_profile(self):
        user = User.objects.create_user(
            username="fresh", email="fresh@example.com", password="StrongPass123"
        )
        self.assertTrue(Profile.objects.filter(user=user).exists())
        # And with both pictures already set, so no component needs a fallback.
        self.assertTrue(user.profile.avatar)
        self.assertTrue(user.profile.cover)


class RequestBodySizeIsEnforcedTests(TestCase):
    """DATA_UPLOAD_MAX_MEMORY_SIZE has to apply to JSON, not just uploads.

    DRF 3.17.1 read the whole body before Django's ceiling was consulted
    (PYSEC-2026-3827), so the 12 MB limit did nothing for a JSON request.
    Pinned to 3.17.2 — and a version pin is easy to loosen by accident.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="huge", email="huge@example.com", password="StrongPass1234"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_an_oversized_json_body_is_refused_with_413(self):
        oversized = "A" * (settings.DATA_UPLOAD_MAX_MEMORY_SIZE + 1024)
        response = self.client.patch(
            "/api/v1/profile/me/", {"bio": oversized}, format="json"
        )
        # 413, not 500: the request is the caller's mistake, and the body
        # never reached a serializer — which is the property under test.
        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(response.data["code"], "request_too_large")
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.bio, "")

    def test_a_normal_body_is_unaffected(self):
        response = self.client.patch(
            "/api/v1/profile/me/", {"bio": "Backend developer."}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class BioIsBoundedTests(TestCase):
    """A profile field is not a file upload.

    With no ceiling the only limit was 12 MB of text per account, read and
    sent again with every public profile view.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="wordy", email="wordy@example.com", password="StrongPass1234"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_an_oversized_bio_is_refused(self):
        from apps.accounts.models import BIO_MAX_LENGTH

        response = self.client.patch(
            "/api/v1/profile/me/", {"bio": "A" * (BIO_MAX_LENGTH + 1)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.bio, "")

    def test_a_normal_bio_is_accepted(self):
        response = self.client.patch(
            "/api/v1/profile/me/", {"bio": "Backend developer."}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ChangeEmailIsGuardedTests(TestCase):
    """Moving the address is how an account is taken permanently.

    Two messages per call, one to an address the caller types — a mail relay
    unless it is rate limited — and it changes the recovery address, so it
    must re-prove who is asking.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="guard", email="guard@example.com", password="StrongPass1234"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def request(self, password="StrongPass1234", email="wanted@example.com"):
        return self.client.post(
            "/api/v1/auth/users/change-email/",
            {"email": email, "current_password": password},
            format="json",
        )

    def test_the_current_password_is_required(self):
        response = self.client.post(
            "/api/v1/auth/users/change-email/", {"email": "wanted@example.com"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.pending_email)

    def test_a_wrong_password_is_refused(self):
        """A stolen access token is no longer enough to start the move."""
        self.assertEqual(self.request(password="NotThePassword").status_code,
                         status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.pending_email)

    def test_the_endpoint_uses_the_tight_mail_budget(self):
        from apps.accounts.views import CustomUserViewSet

        self.assertIn("change_email", CustomUserViewSet.MAIL_SENDING_ACTIONS)
        view = CustomUserViewSet()
        view.action = "change_email"
        self.assertEqual(
            [t.__class__.__name__ for t in view.get_throttles()], ["ScopedRateThrottle"]
        )

    def test_a_burst_of_requests_is_refused(self):
        codes = [self.request(email=f"t{i}@stranger.example.com").status_code
                 for i in range(15)]
        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, codes)

    def test_an_address_pending_for_somebody_else_is_not_reserved(self):
        """Otherwise anyone could hold another person's address for days."""
        other = User.objects.create_user(
            username="squatter", email="squatter@example.com", password="StrongPass1234"
        )
        other.pending_email = "contested@example.com"
        other.save(update_fields=["pending_email"])

        self.assertEqual(self.request(email="contested@example.com").status_code,
                         status.HTTP_200_OK)


class LogoutBelongsToTheCallerTests(TestCase):
    """A refresh token may only be ended by the account it belongs to."""

    def setUp(self):
        cache.clear()
        self.mine = User.objects.create_user(
            username="mine2", email="mine2@example.com", password="StrongPass1234"
        )
        self.theirs = User.objects.create_user(
            username="theirs2", email="theirs2@example.com", password="StrongPass1234"
        )
        self.client = APIClient()

    def refresh_token_for(self, email):
        return self.client.post(
            "/api/v1/auth/jwt/create/",
            {"email": email, "password": "StrongPass1234"},
            format="json",
        ).data["refresh"]

    def test_somebody_elses_token_is_refused(self):
        victim_token = self.refresh_token_for("theirs2@example.com")
        cache.clear()
        self.client.force_authenticate(self.mine)

        response = self.client.post(
            "/api/v1/auth/users/logout/", {"refresh": victim_token}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # And the victim's session is untouched.
        self.client.force_authenticate(None)
        still_valid = self.client.post(
            "/api/v1/auth/jwt/refresh/", {"refresh": victim_token}, format="json"
        )
        self.assertEqual(still_valid.status_code, status.HTTP_200_OK)

    def test_my_own_token_still_works(self):
        mine = self.refresh_token_for("mine2@example.com")
        cache.clear()
        self.client.force_authenticate(self.mine)
        response = self.client.post(
            "/api/v1/auth/users/logout/", {"refresh": mine}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_205_RESET_CONTENT)


class EmailIsCaseInsensitiveTests(TestCase):
    """One mailbox is one account, whatever case it is typed in.

    Phone keyboards capitalise the first letter on their own, and a capital
    letter used to mean "wrong credentials" on sign-in and complete silence
    on password reset.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="cased", email="Cased@Example.COM", password="StrongPass1234"
        )

    def test_the_address_is_stored_lowercase(self):
        self.assertEqual(self.user.email, "cased@example.com")

    def test_sign_in_accepts_any_case(self):
        response = APIClient().post(
            "/api/v1/auth/jwt/create/",
            {"email": "CASED@example.com", "password": "StrongPass1234"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_password_reset_accepts_any_case(self):
        with self.captureOnCommitCallbacks(execute=True):
            APIClient().post(
                "/api/v1/auth/users/reset_password/",
                {"email": "CaSeD@Example.Com"},
                format="json",
            )
        self.assertEqual(len(mail.outbox), 1)

    def test_the_database_refuses_a_second_account_for_one_mailbox(self):
        """The guarantee under the manager, for anything that bypasses it."""
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create(username="dupe2", email="CASED@EXAMPLE.COM")


class StalePendingEmailTests(TestCase):
    """An unconfirmed change must not reserve the address forever.

    ChangeEmailSerializer refuses any address pending for anybody, so a
    request nobody confirms holds that address out of everyone's reach.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="stale", email="stale@example.com", password="StrongPass1234"
        )

    def request_change(self):
        client = APIClient()
        client.force_authenticate(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            client.post(
                "/api/v1/auth/users/change-email/",
                {"email": "wanted@example.com", "current_password": "StrongPass1234"},
                format="json",
            )
        self.user.refresh_from_db()

    def test_the_request_records_when_it_was_made(self):
        self.request_change()
        self.assertEqual(self.user.pending_email, "wanted@example.com")
        self.assertIsNotNone(self.user.pending_email_set_at)

    def test_a_fresh_request_is_left_alone(self):
        from apps.accounts.tasks import clear_stale_pending_emails

        self.request_change()
        self.assertEqual(clear_stale_pending_emails(), 0)
        self.user.refresh_from_db()
        self.assertEqual(self.user.pending_email, "wanted@example.com")

    def test_an_expired_request_releases_the_address(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.accounts.tasks import clear_stale_pending_emails

        self.request_change()
        # Older than the confirmation link's own lifetime.
        User.objects.filter(pk=self.user.pk).update(
            pending_email_set_at=timezone.now()
            - timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT + 60)
        )
        self.assertEqual(clear_stale_pending_emails(), 1)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.pending_email)
        self.assertIsNone(self.user.pending_email_set_at)

    def test_the_released_address_is_available_to_somebody_else(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.accounts.tasks import clear_stale_pending_emails

        self.request_change()
        User.objects.filter(pk=self.user.pk).update(
            pending_email_set_at=timezone.now()
            - timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT + 60)
        )
        clear_stale_pending_emails()

        other = User.objects.create_user(
            username="other", email="other@example.com", password="StrongPass1234"
        )
        client = APIClient()
        client.force_authenticate(other)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                "/api/v1/auth/users/change-email/",
                {"email": "wanted@example.com", "current_password": "StrongPass1234"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class UserWithoutProfileTests(TestCase):
    """A user row can exist with no profile, and must not be a dead account.

    signals.py creates one with every user, but a signal fires on save()
    only. A row from bulk_create, loaddata or raw SQL is a real account, and
    before this it met a 500 on every page of its own.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="orphan", email="orphan@example.com", password="StrongPass1234"
        )
        # Exactly the state a restored backup leaves behind.
        Profile.objects.filter(user=self.user).delete()
        self.user.refresh_from_db()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_the_account_endpoint_answers_instead_of_crashing(self):
        response = self.client.get("/api/v1/auth/users/me/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], "orphan@example.com")
        # No picture to report, and that is a valid answer — not an error.
        self.assertIsNone(response.data["avatar"])

    def test_the_profile_endpoint_creates_the_missing_row(self):
        response = self.client.get("/api/v1/profile/me/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(Profile.objects.filter(user=self.user).exists())
        # And the rule the front end relies on holds again.
        self.assertTrue(response.data["avatar"])

    def test_the_backfill_command_repairs_existing_rows(self):
        from io import StringIO

        from django.core.management import call_command

        self.assertFalse(Profile.objects.filter(user=self.user).exists())
        out = StringIO()
        call_command("backfill_profiles", stdout=out)
        self.assertIn("created 1", out.getvalue())
        self.assertTrue(Profile.objects.filter(user=self.user).exists())

    def test_the_command_is_safe_to_run_twice(self):
        from io import StringIO

        from django.core.management import call_command

        call_command("backfill_profiles", stdout=StringIO())
        out = StringIO()
        call_command("backfill_profiles", stdout=out)
        self.assertIn("already has a profile", out.getvalue())
        self.assertEqual(Profile.objects.filter(user=self.user).count(), 1)


class ConfirmEmailChangeTests(TestCase):
    """A malformed confirmation link is the client's mistake, not a crash."""

    def setUp(self):
        self.client = APIClient()

    def post(self, uid, token="whatever"):
        return self.client.post(
            "/api/v1/auth/users/confirm-email-change/",
            {"uid": uid, "token": token},
            format="json",
        )

    def test_a_garbled_uid_is_a_400_not_a_500(self):
        self.assertEqual(self.post("!!!not-base64!!!").status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_uid_that_is_not_a_number_is_a_400(self):
        # "aGVsbG8=" decodes to "hello", which is not a primary key.
        self.assertEqual(self.post("aGVsbG8=").status_code, status.HTTP_400_BAD_REQUEST)


class EmailChangeFlowTests(TestCase):
    """The address does not move until the new one is confirmed."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="mover", email="old@example.com", password="StrongPass123"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_requesting_a_change_parks_it_and_mails_the_new_address(self):
        # Not decoration: the service queues the mail with
        # transaction.on_commit(), and a TestCase wraps each test in a
        # transaction it rolls back — so that commit never happens and the
        # callback never runs. This runs the callbacks the block collected.
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/auth/users/change-email/",
                {"email": "new@example.com", "current_password": "StrongPass123"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        # Parked, not applied.
        self.assertEqual(self.user.email, "old@example.com")
        self.assertEqual(self.user.pending_email, "new@example.com")

        # Two messages, and which address each went to is the whole point.
        self.assertEqual(len(mail.outbox), 2)
        recipients = {address for message in mail.outbox for address in message.to}
        self.assertEqual(recipients, {"new@example.com", "old@example.com"})

    def test_the_old_address_is_warned_and_the_warning_names_the_new_one(self):
        """The protection that survives a stolen session.

        An attacker with a borrowed session can confirm an address of their
        own — that link goes to them. What they cannot do is stop this
        message reaching the owner.
        """
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                "/api/v1/auth/users/change-email/",
                {"email": "new@example.com", "current_password": "StrongPass123"},
                format="json",
            )
        warning = next(m for m in mail.outbox if m.to == ["old@example.com"])
        self.assertIn("new@example.com", warning.body)
        # A notice, not an action: no link for an attacker to aim at.
        self.assertNotIn("http", warning.body)
        self.assertEqual(warning.alternatives[0][1], "text/html")

    def test_the_new_address_still_gets_a_confirmation_link(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                "/api/v1/auth/users/change-email/",
                {"email": "new@example.com", "current_password": "StrongPass123"},
                format="json",
            )
        confirmation = next(m for m in mail.outbox if m.to == ["new@example.com"])
        self.assertIn("/confirm-email/", confirmation.body)

    def test_an_address_already_taken_is_refused(self):
        User.objects.create_user(
            username="taken", email="taken@example.com", password="StrongPass123"
        )
        response = self.client.post(
            "/api/v1/auth/users/change-email/",
            {"email": "taken@example.com", "current_password": "StrongPass123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class EmailRenderingTests(TestCase):
    """Both versions of every message must render, and carry the link."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="reader", email="reader@example.com",
            password="StrongPass123", first_name="Noor", last_name="Mohsen",
        )

    def test_every_message_has_text_and_html_with_the_same_link(self):
        from apps.accounts.emails import (
            activation_message,
            change_email_message,
            reset_password_message,
        )
        self.user.pending_email = "new@example.com"
        for build in (activation_message, reset_password_message, change_email_message):
            text, html = build(self.user)
            self.assertTrue(text.strip())
            self.assertIn("<html", html)
            # The link must appear in both, or one audience gets a dead end.
            link = [w for w in text.split() if w.startswith("http")][0]
            self.assertIn(link, html)

    def test_the_activation_mail_is_sent_as_both_parts(self):
        from apps.accounts.tasks import send_activation_email
        send_activation_email(self.user.pk)
        sent = mail.outbox[-1]
        self.assertEqual(sent.to, ["reader@example.com"])
        self.assertEqual(len(sent.alternatives), 1)
        self.assertEqual(sent.alternatives[0][1], "text/html")


class DjoserEmailRoutesAreClosedTests(TestCase):
    """Djoser's own email-change endpoints must not exist.

    With USERNAME_FIELD = email, set_username/reset_username become
    set_email/reset_email, which apply the new address immediately with no
    proof it is real — the hole pending_email exists to close.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="dupe", email="dupe@example.com", password="StrongPass1234"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    # 404 or 405: with the action gone, the path falls through to the detail
    # route (`users/<id>/`), which refuses POST. Either way it does not run,
    # and the assertion that matters is that the address did not move.
    REFUSED = {status.HTTP_404_NOT_FOUND, status.HTTP_405_METHOD_NOT_ALLOWED}

    def test_set_email_is_gone(self):
        response = self.client.post(
            "/api/v1/auth/users/set_email/",
            {"current_password": "StrongPass1234", "new_email": "hijack@example.com"},
            format="json",
        )
        self.assertIn(response.status_code, self.REFUSED)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "dupe@example.com")

    def test_reset_email_is_gone(self):
        response = self.client.post(
            "/api/v1/auth/users/reset_email/", {"email": "dupe@example.com"}, format="json"
        )
        self.assertIn(response.status_code, self.REFUSED)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_supported_flow_still_only_parks_the_address(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/auth/users/change-email/",
                {"email": "new@example.com", "current_password": "StrongPass1234"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "dupe@example.com")
        self.assertEqual(self.user.pending_email, "new@example.com")


class SessionsEndWhenCredentialsChangeTests(TestCase):
    """Changing the password has to put an intruder out.

    It is the first thing anyone does when they suspect their account is
    taken. Without revocation the intruder's refresh token stays valid for
    thirty days, so the owner's one action achieves nothing.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="sessions", email="sessions@example.com", password="StrongPass1234"
        )
        self.client = APIClient()

    def sign_in(self, password="StrongPass1234"):
        response = self.client.post(
            "/api/v1/auth/jwt/create/",
            {"email": "sessions@example.com", "password": password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["refresh"]

    def refresh(self, token):
        return self.client.post(
            "/api/v1/auth/jwt/refresh/", {"refresh": token}, format="json"
        ).status_code

    def test_an_access_token_minted_before_the_change_stops_working(self):
        """What CHECK_REVOKE_TOKEN actually guarantees.

        The guarantee is on the *access* token: it carries a hash of the
        password and every request re-checks it, so the intruder's access
        dies on their next call. Testing it with a refresh token proves
        nothing — rotation blacklists that on first use, so the 401 would
        come from the blacklist and pass with this setting removed.
        """
        stolen_access = self.client.post(
            "/api/v1/auth/jwt/create/",
            {"email": "sessions@example.com", "password": "StrongPass1234"},
            format="json",
        ).data["access"]

        working = self.client.get(
            "/api/v1/auth/users/me/", HTTP_AUTHORIZATION=f"JWT {stolen_access}"
        )
        self.assertEqual(working.status_code, status.HTTP_200_OK)

        self.user.set_password("CompletelyNew999")
        self.user.save()

        refused = self.client.get(
            "/api/v1/auth/users/me/", HTTP_AUTHORIZATION=f"JWT {stolen_access}"
        )
        self.assertEqual(refused.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_an_access_token_renewed_from_an_old_refresh_is_dead_too(self):
        """Closes the gap the previous test left open.

        A refresh token is not itself checked against the password hash, so an
        unspent one can still be exchanged — but what comes back carries the
        old hash and is refused on first use.
        """
        stolen_refresh = self.sign_in()

        self.user.set_password("CompletelyNew999")
        self.user.save()

        renewed = self.client.post(
            "/api/v1/auth/jwt/refresh/", {"refresh": stolen_refresh}, format="json"
        )
        if renewed.status_code == status.HTTP_200_OK:
            used = self.client.get(
                "/api/v1/auth/users/me/",
                HTTP_AUTHORIZATION=f"JWT {renewed.data['access']}",
            )
            self.assertEqual(used.status_code, status.HTTP_401_UNAUTHORIZED)
        else:
            self.assertEqual(renewed.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_confirming_an_email_change_also_ends_them(self):
        """The endpoint says "please log in again" — this makes it true."""
        from apps.accounts.emails import build_user_token

        stolen = self.sign_in()
        # Signing in wrote last_login, and that value is part of the token
        # hash — so the in-memory copy has to catch up or the link we build
        # here is stale before it is used.
        self.user.refresh_from_db()
        self.user.pending_email = "moved@example.com"
        self.user.save(update_fields=["pending_email"])
        uid, token = build_user_token(self.user, "confirm_email")

        confirmation = APIClient().post(
            "/api/v1/auth/users/confirm-email-change/",
            {"uid": uid, "token": token},
            format="json",
        )
        self.assertEqual(confirmation.status_code, status.HTTP_200_OK)
        self.assertEqual(self.refresh(stolen), status.HTTP_401_UNAUTHORIZED)

    def test_signing_in_again_works_normally(self):
        """Revocation must not lock the owner out of their own account."""
        self.sign_in()
        self.user.set_password("CompletelyNew999")
        self.user.save()
        fresh = self.sign_in("CompletelyNew999")
        self.assertEqual(self.refresh(fresh), status.HTTP_200_OK)


class PasswordChangedNoticeTests(TestCase):
    """Changing a password tells the owner — through the queue, like the rest."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="notified", email="notified@example.com", password="StrongPass1234"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_the_notice_is_sent_and_carries_both_parts(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/auth/users/set_password/",
                {
                    "current_password": "StrongPass1234",
                    "new_password": "AnotherStrong99",
                    "re_new_password": "AnotherStrong99",
                },
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        sent = mail.outbox[-1]
        self.assertEqual(sent.to, ["notified@example.com"])
        # Both parts, like every other message the project sends.
        self.assertEqual(sent.alternatives[0][1], "text/html")
        # A notice, not an action: no link to click.
        self.assertNotIn("http", sent.body)


class MailIsQueuedAfterCommitTests(TestCase):
    """No account email is queued before its row is committed.

    Hand a worker a job while the transaction is still open and it finds no
    user and dies in the background — so the person waits for an activation
    mail that never arrives.
    """

    def setUp(self):
        cache.clear()

    def test_signup_queues_the_activation_mail_only_on_commit(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = APIClient().post(
                "/api/v1/auth/users/",
                {
                    "first_name": "Noor",
                    "last_name": "Mohsen",
                    "email": "queued@example.com",
                    "password": "VeryStrongPass!99",
                    "re_password": "VeryStrongPass!99",
                },
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            # Still inside the block: the send is a pending callback, and
            # nothing has reached the outbox yet. This is the assertion that
            # fails if someone drops the on_commit wrapper.
            self.assertEqual(len(mail.outbox), 0)

        # `callbacks` is filled when the block exits, not while inside it.
        self.assertEqual(len(callbacks), 1)
        # And the callback has now run, so the mail exists.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["queued@example.com"])

    def test_password_reset_is_queued_the_same_way(self):
        User.objects.create_user(
            username="resetq", email="resetq@example.com", password="StrongPass1234"
        )
        with self.captureOnCommitCallbacks(execute=True):
            APIClient().post(
                "/api/v1/auth/users/reset_password/",
                {"email": "resetq@example.com"},
                format="json",
            )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/reset-password/", mail.outbox[0].body)


class ApiVersionTests(TestCase):
    """The version is part of the path, and only known versions answer."""

    def test_a_known_version_serves(self):
        self.assertEqual(APIClient().get("/api/v1/profile/").status_code, status.HTTP_200_OK)

    def test_an_unknown_version_is_refused(self):
        self.assertEqual(APIClient().get("/api/v9/profile/").status_code, status.HTTP_404_NOT_FOUND)

    def test_health_is_not_versioned(self):
        self.assertEqual(APIClient().get("/api/health/").status_code, status.HTTP_200_OK)


class LoginThrottleTests(TestCase):
    """The sign-in endpoint is the one place guessing pays, so it has a budget."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        User.objects.create_user(
            username="throttled", email="throttled@example.com", password="StrongPass123"
        )

    def test_repeated_failures_are_eventually_refused(self):
        codes = [
            self.client.post(
                "/api/v1/auth/jwt/create/",
                {"email": "throttled@example.com", "password": "wrong"},
                format="json",
            ).status_code
            for _ in range(12)
        ]
        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, codes)


class InactiveAccountLoginTests(TestCase):
    """An unknown email and a wrong password must stay indistinguishable —
    the caller has proved nothing. A correct password on an unactivated
    account is a different case, and answering "no such account" there is
    what strands the owner."""

    def setUp(self):
        # Throttle history lives in the cache, and the throttle test above
        # fills it. Without this, whichever class runs second sees 429.
        cache.clear()
        self.client = APIClient()
        User.objects.create_user(
            username="active_one", email="active@example.com", password="StrongPass123"
        )
        inactive = User.objects.create_user(
            username="inactive_one", email="inactive@example.com", password="StrongPass123"
        )
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])

    def login(self, email, password):
        return self.client.post(
            "/api/v1/auth/jwt/create/", {"email": email, "password": password}, format="json"
        )

    def test_correct_password_on_an_unactivated_account_says_so(self):
        response = self.login("inactive@example.com", "StrongPass123")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["code"], "account_inactive")

    def test_a_wrong_password_never_reveals_that_the_account_exists(self):
        known = self.login("inactive@example.com", "WrongPassword123")
        unknown = self.login("nobody@example.com", "WrongPassword123")
        self.assertEqual(known.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(unknown.status_code, status.HTTP_401_UNAUTHORIZED)
        # Identical down to the body: no differing wording to compare.
        self.assertEqual(known.data, unknown.data)

    def test_an_active_account_still_signs_in(self):
        response = self.login("active@example.com", "StrongPass123")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_an_unactivated_account_gets_no_tokens(self):
        response = self.login("inactive@example.com", "StrongPass123")
        self.assertNotIn("access", response.data)
        self.assertNotIn("refresh", response.data)


class ProfilePictureClearingTests(TestCase):
    """Removing a picture must land on the default, not on nothing."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="pictures", email="pictures@example.com", password="StrongPass123"
        )
        self.client.force_authenticate(self.user)
        self.profile = self.user.profile

    def default_for(self, field):
        return Profile._meta.get_field(field).default

    def test_clearing_an_avatar_restores_the_default(self):
        self.profile.avatar = "user/avatars/something-custom.png"
        self.profile.save(update_fields=["avatar"])

        response = self.client.patch("/api/v1/profile/me/", {"clear_avatar": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.avatar.name, self.default_for("avatar"))

    def test_clearing_a_cover_restores_the_default(self):
        self.profile.cover = "user/cover/something-custom.png"
        self.profile.save(update_fields=["cover"])

        response = self.client.patch("/api/v1/profile/me/", {"clear_cover": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.cover.name, self.default_for("cover"))

    def test_not_asking_to_clear_leaves_the_picture_alone(self):
        self.profile.avatar = "user/avatars/keep-me.png"
        self.profile.save(update_fields=["avatar"])

        response = self.client.patch("/api/v1/profile/me/", {"bio": "hello"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.avatar.name, "user/avatars/keep-me.png")

    def test_a_profile_always_reports_a_picture(self):
        """The rule the front end is allowed to rely on."""
        self.client.patch("/api/v1/profile/me/", {"clear_avatar": True}, format="json")
        response = self.client.get("/api/v1/profile/me/")
        self.assertTrue(response.data["avatar"])
        self.assertTrue(response.data["cover"])

    def test_the_verified_badge_cannot_be_self_awarded(self):
        self.client.patch("/api/v1/profile/me/", {"is_identity_verified": True}, format="json")
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_identity_verified)


class ShrinkTests(SimpleTestCase):
    """Builds a picture in memory, runs it through `shrink`, reads it back."""

    def upload(self, width, height, fmt="JPEG"):
        buffer = BytesIO()
        Image.new("RGB", (width, height), "teal").save(buffer, format=fmt)
        return SimpleUploadedFile(f"photo.{fmt.lower()}", buffer.getvalue())

    def test_large_picture_is_capped_and_re_encoded_as_webp(self):
        result = shrink(self.upload(4000, 2500), 1600)
        stored = Image.open(BytesIO(result.read()))
        self.assertEqual(stored.format, "WEBP")
        self.assertEqual(stored.size, (1600, 1000))
        self.assertTrue(result.name.endswith(".webp"))

    def test_small_picture_is_never_enlarged(self):
        result = shrink(self.upload(300, 200), 1600)
        self.assertEqual(Image.open(BytesIO(result.read())).size, (300, 200))

    def test_non_image_is_refused(self):
        """A .jpg that is not a JPEG never reaches storage — the extension
        is only a claim, and this is the check that does not take it."""
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            shrink(SimpleUploadedFile("photo.jpg", b"not an image at all"), 1600)


class DecompressionBombTests(SimpleTestCase):
    """A small file can still be a huge image.

    The size ceiling counts bytes; this counts pixels. A flat-colour PNG at
    13000x13000 is a few hundred kilobytes on disk and hundreds of megabytes
    decoded — two concurrent uploads can OOM a small server.
    """

    def png(self, width, height):
        buffer = BytesIO()
        # Pillow refuses to *create* one this big without the ceiling
        # lifted; an attacker's file has no such scruples.
        previous, Image.MAX_IMAGE_PIXELS = Image.MAX_IMAGE_PIXELS, None
        try:
            Image.new("L", (width, height), 0).save(buffer, format="PNG")
        finally:
            Image.MAX_IMAGE_PIXELS = previous
        return SimpleUploadedFile("bomb.png", buffer.getvalue())

    def test_a_small_file_with_too_many_pixels_is_refused(self):
        from rest_framework.exceptions import ValidationError

        from apps.accounts.validators.image import MAX_IMAGE_PIXELS

        upload = self.png(9000, 9000)  # 81M pixels, well under 10 MB
        self.assertLess(upload.size, 10 * 1024 * 1024)
        self.assertGreater(9000 * 9000, MAX_IMAGE_PIXELS)

        with self.assertRaises(ValidationError):
            shrink(upload, 1600)

    def test_pillows_own_bomb_error_is_a_400_not_a_500(self):
        """DecompressionBombError is not an OSError, so it used to escape."""
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            shrink(self.png(15000, 15000), 1600)

    def test_an_ordinary_photograph_still_passes(self):
        buffer = BytesIO()
        Image.new("RGB", (4000, 3000), "teal").save(buffer, format="JPEG")
        result = shrink(SimpleUploadedFile("photo.jpg", buffer.getvalue()), 1600)
        self.assertEqual(Image.open(BytesIO(result.read())).size, (1600, 1200))


class SignupIsRateLimitedTests(TestCase):
    """Signup sends mail, so it gets the tight budget, not the loose one."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_create_uses_the_auth_scope(self):
        from apps.accounts.views import CustomUserViewSet

        self.assertIn("create", CustomUserViewSet.MAIL_SENDING_ACTIONS)
        view = CustomUserViewSet()
        view.action = "create"
        self.assertEqual(
            [t.__class__.__name__ for t in view.get_throttles()], ["ScopedRateThrottle"]
        )

    def test_a_burst_of_signups_is_refused(self):
        codes = []
        for i in range(15):
            codes.append(
                self.client.post(
                    "/api/v1/auth/users/",
                    {
                        "first_name": "Noor",
                        "last_name": "Mohsen",
                        "email": f"burst{i}@example.com",
                        "password": "VeryStrongPass!99",
                        "re_password": "VeryStrongPass!99",
                    },
                    format="json",
                ).status_code
            )
        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, codes)


class SchemaIsNotPublicInProductionTests(SimpleTestCase):
    """The API map is for staff on a server, for everyone on a laptop."""

    def load_settings(self, **env):
        import importlib.util
        import os
        from pathlib import Path
        from unittest import mock

        path = Path(settings.BASE_DIR) / "config" / "settings.py"
        spec = importlib.util.spec_from_file_location("schema_probe", path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, env):
            spec.loader.exec_module(module)
        return module

    def test_production_serves_the_schema_to_staff_only(self):
        loaded = self.load_settings(DEBUG="False", ALLOWED_HOSTS="example.com")
        self.assertEqual(
            loaded.SPECTACULAR_SETTINGS["SERVE_PERMISSIONS"],
            ["rest_framework.permissions.IsAdminUser"],
        )

    def test_development_leaves_it_open(self):
        loaded = self.load_settings(DEBUG="True")
        self.assertEqual(
            loaded.SPECTACULAR_SETTINGS["SERVE_PERMISSIONS"],
            ["rest_framework.permissions.AllowAny"],
        )


class SeedMediaDefaultsTests(SimpleTestCase):
    """The two default pictures must land exactly where the model points."""

    def test_defaults_are_copied_into_media_root(self):
        call_command("seed_media_defaults", verbosity=0)
        for field in ("avatar", "cover"):
            path = Path(settings.MEDIA_ROOT) / Profile._meta.get_field(field).default
            self.assertTrue(path.exists(), f"{field} default missing at {path}")
            self.assertGreater(path.stat().st_size, 1000)


class PeopleSearchTests(TestCase):
    """`/api/v1/profile/?search=` finds people; with no term it is not a directory."""

    def setUp(self):
        cache.clear()
        for username, first, last in (
            ("noor", "Noor", "Mohsen"),
            ("hasen", "Hasen", "Bader"),
            ("ghost", "Gone", "Away"),
        ):
            User.objects.create_user(
                username=username,
                email=f"{username}@example.com",
                password="StrongPass123",
                first_name=first,
                last_name=last,
            )
        User.objects.filter(username="ghost").update(is_active=False)
        self.client = APIClient()

    def results(self, query=""):
        return self.client.get("/api/v1/profile/", {"search": query} if query else {}).data["results"]

    def test_no_term_lists_nobody(self):
        self.assertEqual(self.results(), [])
        self.assertEqual(self.results("n"), [])

    def test_username_and_names_match_by_prefix(self):
        self.assertEqual([r["username"] for r in self.results("noo")], ["noor"])
        self.assertEqual([r["username"] for r in self.results("Bader")], ["hasen"])

    def test_inactive_accounts_are_not_found(self):
        self.assertEqual(self.results("gho"), [])


class TokensAreNotInterchangeableTests(TestCase):
    """A token minted for one flow must be refused by every other.

    Django's default generator encodes no purpose, and djoser uses it for
    activation and password reset. Sharing it for email confirmation let a
    link sent to an unverified address be replayed against
    reset_password_confirm — handing over the password.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="tokens", email="tokens@example.com", password="StrongPass1234"
        )
        self.user.pending_email = "attacker@example.com"
        self.user.save(update_fields=["pending_email"])

    def email_change_token_for(self, user):
        from apps.accounts.emails import build_user_token

        return build_user_token(user, "confirm_email")

    def test_an_email_change_token_cannot_reset_the_password(self):
        """The attack the separation closes."""
        uid, token = self.email_change_token_for(self.user)
        response = APIClient().post(
            "/api/v1/auth/users/reset_password_confirm/",
            {
                "uid": uid,
                "token": token,
                "new_password": "AttackerOwns99!",
                "re_new_password": "AttackerOwns99!",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password("AttackerOwns99!"))

    def test_a_password_reset_token_cannot_confirm_an_email_change(self):
        """And the reverse: a leaked reset link must not move the address."""
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        response = APIClient().post(
            "/api/v1/auth/users/confirm-email-change/",
            {"uid": uid, "token": token},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "tokens@example.com")

    def test_the_real_confirmation_still_works(self):
        uid, token = self.email_change_token_for(self.user)
        response = APIClient().post(
            "/api/v1/auth/users/confirm-email-change/",
            {"uid": uid, "token": token},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "attacker@example.com")

    def test_asking_for_a_different_address_kills_the_older_link(self):
        """pending_email is in the hash, so one request at a time is valid."""
        uid, first_token = self.email_change_token_for(self.user)

        self.user.pending_email = "second@example.com"
        self.user.save(update_fields=["pending_email"])

        response = APIClient().post(
            "/api/v1/auth/users/confirm-email-change/",
            {"uid": uid, "token": first_token},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "tokens@example.com")


class ThrottleCannotBeBypassedTests(TestCase):
    """The rate limit must survive a forged X-Forwarded-For.

    Nginx passes the visitor's real address in that header, so DRF has to
    read it — but without NUM_PROXIES it uses the whole string, including the
    part the client sent, and a rotated header buys a fresh counter every
    request. Two decisions rest on this limit in writing: the "not activated"
    message in LoginSerializer, and silencing axes.W006 in settings.
    """

    def setUp(self):
        cache.clear()
        User.objects.create_user(
            username="bypass", email="bypass@example.com", password="StrongPass1234"
        )
        self.client = APIClient()

    def attempt(self, **extra):
        return self.client.post(
            "/api/v1/auth/jwt/create/",
            {"email": "bypass@example.com", "password": "wrong"},
            format="json",
            **extra,
        ).status_code

    def test_the_throttle_key_ignores_what_the_client_sent(self):
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory
        from rest_framework.throttling import AnonRateThrottle

        factory = APIRequestFactory()
        throttle = AnonRateThrottle()
        first = throttle.get_ident(
            Request(factory.get("/", HTTP_X_FORWARDED_FOR="1.1.1.1, 203.0.113.9"))
        )
        second = throttle.get_ident(
            Request(factory.get("/", HTTP_X_FORWARDED_FOR="2.2.2.2, 203.0.113.9"))
        )
        # Same real client (the last hop), so the same key — whatever the
        # forged prefix says.
        self.assertEqual(first, second)
        self.assertEqual(first, "203.0.113.9")

    def test_a_rotating_header_still_gets_throttled(self):
        codes = [
            self.attempt(HTTP_X_FORWARDED_FOR=f"9.9.9.{i}, 203.0.113.9")
            for i in range(15)
        ]
        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, codes)

    def test_the_limit_still_works_without_any_header(self):
        codes = [self.attempt() for _ in range(15)]
        self.assertIn(status.HTTP_429_TOO_MANY_REQUESTS, codes)


class FrontendPathsHaveOneSourceTests(TestCase):
    """The activation page is defined once, and both builders read it.

    Written twice, once for djoser and once for emails.py, moving the page
    means editing two files — and half the mail then carries a dead link
    with nothing raising an error.
    """

    def test_djoser_reads_the_shared_paths(self):
        from djoser.conf import settings as djoser_settings

        self.assertEqual(djoser_settings.ACTIVATION_URL, settings.FRONTEND_PATHS["activate"])
        self.assertEqual(
            djoser_settings.PASSWORD_RESET_CONFIRM_URL,
            settings.FRONTEND_PATHS["reset_password"],
        )

    def test_our_links_are_built_from_the_same_paths(self):
        from apps.accounts.emails import build_link

        user = User.objects.create_user(
            username="linked", email="linked@example.com", password="StrongPass1234"
        )
        for page in ("activate", "reset_password", "confirm_email"):
            prefix = settings.FRONTEND_PATHS[page].split("/")[0]
            self.assertIn(f"/{prefix}/", build_link(user, page))

    def test_an_unknown_page_fails_loudly(self):
        """A typo is a KeyError at the call, not a silent broken link."""
        from apps.accounts.emails import build_link

        user = User.objects.create_user(
            username="loud", email="loud@example.com", password="StrongPass1234"
        )
        with self.assertRaises(KeyError):
            build_link(user, "no-such-page")


class AxesWiringTests(SimpleTestCase):
    """The lockout switch, both positions.

    settings.py is executed as a plain module with the variable set each way
    — no Django boot, no database. That is the only way to see INSTALLED_APPS
    and AUTHENTICATION_BACKENDS under a flag read at import time.
    """

    def load_settings(self, **env):
        import importlib.util
        import os
        from pathlib import Path
        from unittest import mock

        path = Path(settings.BASE_DIR) / "config" / "settings.py"
        spec = importlib.util.spec_from_file_location("settings_probe", path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, env):
            spec.loader.exec_module(module)
        return module

    def test_off_by_default_changes_nothing(self):
        loaded = self.load_settings(AXES_ENABLED="False")
        self.assertFalse(loaded.AXES_ENABLED)
        self.assertNotIn("axes", loaded.INSTALLED_APPS)
        self.assertNotIn("axes.middleware.AxesMiddleware", loaded.MIDDLEWARE)
        self.assertEqual(
            loaded.AUTHENTICATION_BACKENDS,
            ["django.contrib.auth.backends.AllowAllUsersModelBackend"],
        )

    def test_on_installs_the_app_the_middleware_and_the_backend(self):
        loaded = self.load_settings(AXES_ENABLED="True")
        self.assertTrue(loaded.AXES_ENABLED)
        self.assertIn("axes", loaded.INSTALLED_APPS)
        self.assertEqual(loaded.MIDDLEWARE[-1], "axes.middleware.AxesMiddleware")
        self.assertEqual(loaded.AXES_FAILURE_LIMIT, 5)
        self.assertEqual(loaded.AXES_LOCKOUT_PARAMETERS, ["username"])

    def test_the_inactive_account_backend_survives_behind_axes(self):
        """The line that breaks the "not activated" message if it is dropped.

        Axes first, so a locked account is refused before any password check;
        AllowAllUsersModelBackend behind it, because that is what hands
        LoginSerializer an inactive user instead of None.
        """
        backends = self.load_settings(AXES_ENABLED="True").AUTHENTICATION_BACKENDS
        self.assertEqual(backends[0], "axes.backends.AxesStandaloneBackend")
        self.assertIn(
            "django.contrib.auth.backends.AllowAllUsersModelBackend", backends[1:]
        )


@unittest.skipUnless(
    settings.AXES_ENABLED,
    "run with AXES_ENABLED=True — CI has a job that does",
)
class AxesLockoutTests(TestCase):
    """The lockout itself, exercised for real.

    Skipped unless the flag is on, because axes has to be in INSTALLED_APPS
    when Django boots. CI runs the suite a second time with it on.
    """

    def setUp(self):
        cache.clear()
        from axes.handlers.proxy import AxesProxyHandler

        AxesProxyHandler.reset_attempts()
        User.objects.create_user(
            username="locked", email="locked@example.com", password="StrongPass1234"
        )
        self.client = APIClient()

    def login(self, password):
        return self.client.post(
            "/api/v1/auth/jwt/create/",
            {"email": "locked@example.com", "password": password},
            format="json",
        )

    def test_the_account_locks_after_five_failures(self):
        for _ in range(5):
            self.login("wrong")
        # The right password now, and it is still refused: the lock is on
        # the account, not on the guess.
        self.assertEqual(self.login("StrongPass1234").status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_different_account_is_untouched(self):
        User.objects.create_user(
            username="bystander", email="bystander@example.com", password="StrongPass1234"
        )
        for _ in range(5):
            self.login("wrong")
        response = self.client.post(
            "/api/v1/auth/jwt/create/",
            {"email": "bystander@example.com", "password": "StrongPass1234"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ABannedAccountStaysBannedTests(TestCase):
    """Every path that can set is_active has to know about is_banned.

    The field was added for the Google path and checked only there, while
    activation and the admin's unban action also set is_active — so a banned
    person only had to ask for a fresh activation link.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="banned3", email="banned3@example.com", password="StrongPass1234"
        )
        self.user.last_login = timezone.now()
        self.user.save(update_fields=["last_login"])
        self.user.is_banned = True
        self.user.is_active = False
        self.user.save(update_fields=["is_banned", "is_active"])
        self.client = APIClient()

    def test_sign_in_does_not_point_them_at_activation(self):
        response = self.client.post(
            "/api/v1/auth/jwt/create/",
            {"email": "banned3@example.com", "password": "StrongPass1234"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["code"], "account_disabled")

    def test_no_activation_link_is_sent(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/auth/users/resend_activation/",
                {"email": "banned3@example.com"},
                format="json",
            )
        # Answered like any unknown address, so it confirms nothing either.
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(len(mail.outbox), 0)

    def test_no_password_reset_link_is_sent(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                "/api/v1/auth/users/reset_password/",
                {"email": "banned3@example.com"},
                format="json",
            )
        self.assertEqual(len(mail.outbox), 0)

    def test_an_activation_link_issued_before_the_ban_no_longer_works(self):
        """The shorter route: a link already sitting in their inbox."""
        from apps.accounts.emails import build_user_token

        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        uid, token = build_user_token(self.user, "activate")

        response = self.client.post(
            "/api/v1/auth/users/activation/", {"uid": uid, "token": token}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)


class BanningIsEnforcedOnSaveTests(TestCase):
    """Ticking `is_banned` has to ban, wherever it is ticked.

    The Ban action sets both flags, but the same field is a checkbox on the
    edit page, and ticking it there left `is_active` alone. save() is the one
    place that cannot be bypassed.
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="ticked", email="ticked@example.com", password="StrongPass1234"
        )

    def test_setting_the_flag_alone_deactivates_the_account(self):
        self.user.is_banned = True
        self.user.save()
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_it_works_with_update_fields_too(self):
        """update_fields has to grow, or the change is computed and dropped."""
        self.user.is_banned = True
        self.user.save(update_fields=["is_banned"])
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_banned is False)
        self.assertFalse(self.user.is_active)

    def test_a_banned_account_cannot_sign_in_afterwards(self):
        self.user.is_banned = True
        self.user.save()
        response = APIClient().post(
            "/api/v1/auth/jwt/create/",
            {"email": "ticked@example.com", "password": "StrongPass1234"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["code"], "account_disabled")

    def test_an_ordinary_save_leaves_an_active_account_alone(self):
        self.user.first_name = "Renamed"
        self.user.save()
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)


class DuplicateEmailIsNamedClearlyTests(TestCase):
    """Signing up with a taken address says so, in any case."""

    def setUp(self):
        cache.clear()
        User.objects.create_user(
            username="boss", email="boss@example.com", password="StrongPass1234"
        )
        self.client = APIClient()

    def signup(self, email):
        return self.client.post(
            "/api/v1/auth/users/",
            {
                "first_name": "Noor",
                "last_name": "Mohsen",
                "email": email,
                "password": "VeryStrongPass!99",
                "re_password": "VeryStrongPass!99",
            },
            format="json",
        )

    def test_the_same_address_is_refused_by_name(self):
        response = self.signup("boss@example.com")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)

    def test_a_different_case_is_refused_the_same_way(self):
        """DRF's uniqueness check compares the address as typed, so this used
        to fall through to the database and come back as a generic failure
        with no field named."""
        response = self.signup("BOSS@Example.com")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)
        self.assertEqual(User.objects.filter(email__iexact="boss@example.com").count(), 1)


class ActivationLinkIsSingleUseTests(TestCase):
    """Djoser refuses a link for an account that is already active.

    Replacing its validate() to swap the token generator dropped that check.
    Alone it only re-activates what is active, but any signal hung off
    activation later would fire once per click.
    """

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_the_second_use_of_a_link_is_refused(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                "/api/v1/auth/users/",
                {
                    "first_name": "Once",
                    "last_name": "Only",
                    "email": "once@example.com",
                    "password": "VeryStrongPass!99",
                    "re_password": "VeryStrongPass!99",
                },
                format="json",
            )
        uid, token = re.search(
            r"/activate/([^/]+)/(\S+)", mail.outbox[-1].body
        ).groups()

        first = self.client.post(
            "/api/v1/auth/users/activation/", {"uid": uid, "token": token}, format="json"
        )
        self.assertEqual(first.status_code, status.HTTP_204_NO_CONTENT)

        second = self.client.post(
            "/api/v1/auth/users/activation/", {"uid": uid, "token": token}, format="json"
        )
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)


class UnbanRestoresOnlyWhatTheBanTookTests(TestCase):
    """Lifting a ban must not hand out a confirmed status nobody earned."""

    def setUp(self):
        cache.clear()
        self.admin = admin.site._registry[User]
        self.request = RequestFactory().get("/")
        self.request.session = {}
        self.request._messages = FallbackStorage(self.request)

    def banned_user(self, username, *, had_signed_in):
        user = User.objects.create_user(
            username=username, email=f"{username}@example.com", password="StrongPass1234"
        )
        if had_signed_in:
            user.last_login = timezone.now()
            user.save(update_fields=["last_login"])
        User.objects.filter(pk=user.pk).update(is_banned=True, is_active=False)
        return user

    def test_an_account_that_had_signed_in_comes_back(self):
        user = self.banned_user("returning", had_signed_in=True)
        self.admin.unban_users(self.request, User.objects.filter(pk=user.pk))
        user.refresh_from_db()
        self.assertFalse(user.is_banned)
        self.assertTrue(user.is_active)

    def test_an_account_that_never_did_stays_inactive(self):
        user = self.banned_user("neverwas", had_signed_in=False)
        self.admin.unban_users(self.request, User.objects.filter(pk=user.pk))
        user.refresh_from_db()
        self.assertFalse(user.is_banned)
        # The ban is lifted, but the email is still unconfirmed.
        self.assertFalse(user.is_active)


class GoogleSignInTests(TestCase):
    """A whole authentication path that had no test at all.

    The Google token itself is mocked — verifying a real one needs Google.
    What is exercised is everything after: what account comes back, what
    state it is in, and who is refused.
    """

    GOOGLE_PAYLOAD = {
        "email": "g@example.com",
        "email_verified": True,
        "given_name": "Noor",
        "family_name": "Mohsen",
    }

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def sign_in(self, payload=None):
        from unittest.mock import patch

        with patch(
            "apps.accounts.views.verify_google_token",
            return_value=payload or self.GOOGLE_PAYLOAD,
        ):
            return self.client.post("/api/v1/google/", {"token": "irrelevant"}, format="json")

    def test_a_new_address_becomes_an_active_account_with_a_profile(self):
        response = self.sign_in()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(response.data), {"access", "refresh"})

        user = User.objects.get(email="g@example.com")
        # No activation mail: Google already proved the address is theirs.
        self.assertTrue(user.is_active)
        self.assertTrue(hasattr(user, "profile"))

    def test_the_account_has_no_password_rather_than_a_blank_one(self):
        """set_unusable_password, and it must survive the create.

        A blank password reads as *usable* to Django, which would refuse
        the owner set-new-password and leave them unable to set one at all.
        """
        self.sign_in()
        user = User.objects.get(email="g@example.com")
        self.assertFalse(user.has_usable_password())

        me = self.client.get(
            "/api/v1/auth/users/me/",
            HTTP_AUTHORIZATION=f"JWT {self.sign_in().data['access']}",
        )
        self.assertFalse(me.data["has_usable_password"])

    def test_signing_in_twice_reuses_the_same_account(self):
        self.sign_in()
        self.sign_in()
        self.assertEqual(User.objects.filter(email="g@example.com").count(), 1)

    def test_a_mixed_case_address_does_not_create_a_second_account(self):
        self.sign_in()
        self.sign_in({**self.GOOGLE_PAYLOAD, "email": "G@Example.COM"})
        self.assertEqual(User.objects.filter(email="g@example.com").count(), 1)

    def test_a_pre_registered_account_loses_the_attackers_password(self):
        """Pre-account takeover, and why activation alone is not enough.

        An attacker registers with the victim's address and their own
        password; the account stays inactive because the mail went to the
        victim. Months later the victim signs in with Google and the account
        goes live — and a surviving password is a working key to it. Nobody
        proved that password belonged to the address holder.
        """
        registration = self.client.post(
            "/api/v1/auth/users/",
            {
                "first_name": "Vic",
                "last_name": "Tim",
                "email": "g@example.com",
                "password": "AttackerKnows99!",
                "re_password": "AttackerKnows99!",
            },
            format="json",
        )
        self.assertEqual(registration.status_code, status.HTTP_201_CREATED)

        self.assertEqual(self.sign_in().status_code, status.HTTP_200_OK)

        user = User.objects.get(email="g@example.com")
        self.assertTrue(user.is_active)
        # The account is the victim's now, and has no password at all.
        self.assertFalse(user.check_password("AttackerKnows99!"))
        self.assertFalse(user.has_usable_password())

        cache.clear()
        attacker = self.client.post(
            "/api/v1/auth/jwt/create/",
            {"email": "g@example.com", "password": "AttackerKnows99!"},
            format="json",
        )
        self.assertEqual(attacker.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_banned_account_is_not_let_back_in(self):
        """is_active carries two meanings; is_banned says which one."""
        user = User.objects.create_user(
            username="banned", email="g@example.com", password="StrongPass1234"
        )
        user.is_banned = True
        user.is_active = False
        user.save(update_fields=["is_banned", "is_active"])

        response = self.sign_in()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_an_account_that_has_been_used_is_not_reactivated(self):
        """Switched off after use was a deliberate act, whatever the reason."""
        from django.utils import timezone

        user = User.objects.create_user(
            username="retired", email="g@example.com", password="StrongPass1234"
        )
        user.last_login = timezone.now()
        user.is_active = False
        user.save(update_fields=["last_login", "is_active"])

        response = self.sign_in()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_an_unactivated_account_is_activated_rather_than_refused(self):
        """Google proves the address; activation asks for nothing more.

        Someone who signed up, never opened the mail, then came back through
        Google used to be locked out of an account they had just proved
        they own.
        """
        user = User.objects.create_user(
            username="dormant", email="g@example.com", password="StrongPass1234"
        )
        user.is_active = False
        user.save(update_fields=["is_active"])

        response = self.sign_in()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_an_unverified_google_address_is_refused(self):
        from unittest.mock import patch

        from rest_framework.exceptions import ValidationError

        from apps.accounts.google_auth import verify_google_token

        with patch("apps.accounts.google_auth.id_token.verify_oauth2_token",
                   return_value={"email": "x@example.com", "email_verified": False}):
            with self.settings(GOOGLE_CLIENT_ID="test-client-id"):
                with self.assertRaises(ValidationError):
                    verify_google_token("token")

    def test_it_refuses_cleanly_when_google_is_not_configured(self):
        from rest_framework.exceptions import ValidationError

        from apps.accounts.google_auth import verify_google_token

        with self.settings(GOOGLE_CLIENT_ID=""):
            with self.assertRaises(ValidationError):
                verify_google_token("token")


class LogoutTests(TestCase):
    """Signing out has to actually end the session."""

    def setUp(self):
        cache.clear()
        User.objects.create_user(
            username="leaver", email="leaver@example.com", password="StrongPass1234"
        )
        self.client = APIClient()
        response = self.client.post(
            "/api/v1/auth/jwt/create/",
            {"email": "leaver@example.com", "password": "StrongPass1234"},
            format="json",
        )
        self.access = response.data["access"]
        self.refresh = response.data["refresh"]

    def test_the_refresh_token_stops_working(self):
        response = self.client.post(
            "/api/v1/auth/users/logout/",
            {"refresh": self.refresh},
            format="json",
            HTTP_AUTHORIZATION=f"JWT {self.access}",
        )
        self.assertEqual(response.status_code, status.HTTP_205_RESET_CONTENT)

        again = self.client.post(
            "/api/v1/auth/jwt/refresh/", {"refresh": self.refresh}, format="json"
        )
        self.assertEqual(again.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_junk_token_is_a_400_not_a_500(self):
        response = self.client.post(
            "/api/v1/auth/users/logout/",
            {"refresh": "not-a-token"},
            format="json",
            HTTP_AUTHORIZATION=f"JWT {self.access}",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SetNewPasswordGuardTests(TestCase):
    """The check that keeps this endpoint from being a free password reset."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="guarded", email="guarded@example.com", password="StrongPass1234"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_an_account_that_has_a_password_is_refused(self):
        response = self.client.post(
            "/api/v1/auth/users/set-new-password/",
            {"new_password": "SomethingElse123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "password_already_set")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("StrongPass1234"))

    def test_an_account_without_one_may_set_it(self):
        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])

        response = self.client.post(
            "/api/v1/auth/users/set-new-password/",
            {"new_password": "SomethingElse123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("SomethingElse123"))

    def test_the_project_password_policy_applies(self):
        """min_length lives in one place — the validators, not the field."""
        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])

        response = self.client.post(
            "/api/v1/auth/users/set-new-password/", {"new_password": "Short1a"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OneUserCannotTouchAnotherTests(TestCase):
    """Guarded by architecture today; this makes that deliberate.

    ProfileViewSet is a ReadOnlyModelViewSet, so no write route to another
    person's profile is generated at all. Nothing stops a later change to
    ModelViewSet except a test that notices.
    """

    def setUp(self):
        cache.clear()
        self.mine = User.objects.create_user(
            username="mine", email="mine@example.com", password="StrongPass1234"
        )
        self.theirs = User.objects.create_user(
            username="theirs", email="theirs@example.com", password="StrongPass1234"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.mine)

    def test_no_write_route_to_another_profile_exists(self):
        for method in (self.client.patch, self.client.put, self.client.delete):
            response = method("/api/v1/profile/theirs/", {"bio": "changed"}, format="json")
            self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.theirs.profile.refresh_from_db()
        self.assertEqual(self.theirs.profile.bio, "")

    def test_writing_to_me_only_touches_my_own_row(self):
        self.client.patch("/api/v1/profile/me/", {"bio": "mine only"}, format="json")
        self.mine.profile.refresh_from_db()
        self.theirs.profile.refresh_from_db()
        self.assertEqual(self.mine.profile.bio, "mine only")
        self.assertEqual(self.theirs.profile.bio, "")


class HealthEndpointTests(TestCase):
    """The monitor's endpoint: open, and honest about the dependencies."""

    def test_it_is_reachable_without_a_token_and_reports_ok(self):
        response = APIClient().get("/api/health/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ok")
        self.assertEqual(response.data["database"], "ok")


class ErrorShapeTests(TestCase):
    """Every error body carries a `code` the client can branch on."""

    def test_an_unauthenticated_request_has_a_code(self):
        response = APIClient().get("/api/v1/profile/me/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("code", response.data)

    def test_a_missing_page_is_a_clean_404(self):
        response = APIClient().get("/api/v1/profile/nobody-by-that-name/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("code", response.data)
