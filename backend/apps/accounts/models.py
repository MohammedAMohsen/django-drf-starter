"""The user, and everything about them that is not authentication.

Two models on purpose: User holds identity and the fields Django's auth
machinery touches, Profile holds what the interface shows — so the profile
can grow without widening the row every authenticated request loads.

USERNAME_FIELD is the email, so the username is a public handle instead of a
credential. It is generated at signup rather than asked for — see services.py.
"""

from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.db.models.functions import Lower

BIO_MAX_LENGTH = 500


class CaseInsensitiveUserManager(UserManager):
    """Lowercases the address on every account this manager creates.

    Django's normalize_email only lowercases the domain, so `A@B.com` stayed
    `A@b.com` and one mailbox could hold two accounts. Overriding here covers
    the API, createsuperuser, the admin and scripts.
    """

    @classmethod
    def normalize_email(cls, email):
        return super().normalize_email(email).lower()


class User(AbstractUser):
    # unique is not Django's default here, and it is what makes the address
    # usable as the sign-in credential.
    email = models.EmailField(unique=True)

    # A change of address waits here until confirmed, so a typo cannot lock
    # the owner out and a borrowed session cannot move the account outright.
    #
    # Deliberately NOT unique: that let anyone reserve another person's
    # address for days by asking to move to it and never confirming.
    # confirm_user_email checks again at the last step.
    pending_email = models.EmailField(blank=True, null=True)

    # Without this the address stayed reserved forever, even after the token
    # expired. apps.accounts.tasks.clear_stale_pending_emails clears them.
    pending_email_set_at = models.DateTimeField(blank=True, null=True)

    # Required, unlike Django's defaults.
    first_name = models.CharField(max_length=150, blank=False)
    last_name = models.CharField(max_length=150, blank=False)

    # `is_active=False` means both "has not confirmed their email" and "an
    # administrator shut this down". This field says which, so nothing turns
    # the wrong one back on — the Google path once un-banned a banned account.
    is_banned = models.BooleanField(
        default=False,
        help_text=(
            "Shut down by an administrator. Blocks every way back in, "
            "including Google sign-in."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    # What createsuperuser asks for on top of the email and password.
    REQUIRED_FIELDS = ["username"]

    objects = CaseInsensitiveUserManager()

    class Meta:
        # `-id` as a tiebreaker: rows sharing created_at would otherwise
        # appear on two pages of one listing, or on neither.
        ordering = ["-created_at", "-id"]
        constraints = [
            # The database itself refuses a second row differing only in case.
            models.UniqueConstraint(Lower("email"), name="user_email_ci_unique"),
        ]

    def save(self, *args, **kwargs):
        """A banned account cannot be active.

        `is_banned` is also a checkbox on the admin edit page, and ticking it
        there left `is_active` alone — the account kept working. update_fields
        has to grow to match, or the change is computed and never written.
        """
        if self.is_banned and self.is_active:
            self.is_active = False
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "is_active" not in update_fields:
                kwargs["update_fields"] = [*update_fields, "is_active"]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class Profile(models.Model):
    """Created automatically for every user — see signals.py.

    Nothing here is required, so a profile always exists; the pictures have
    defaults for the same reason, so no component needs a fallback. Both
    files must exist under MEDIA_ROOT — `manage.py seed_media_defaults`.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    avatar = models.ImageField(
        default="user/avatars/default.png", upload_to="user/avatars/", blank=True
    )
    cover = models.ImageField(
        default="user/cover/defaultCover.png", upload_to="user/cover/", blank=True
    )
    # blank=True without null=True: on a text field null=True gives two ways
    # to be empty. max_length here is enforced by serializers, not PostgreSQL
    # — enough, and without it the only ceiling was 12 MB of text per account.
    bio = models.TextField(blank=True, max_length=BIO_MAX_LENGTH)
    birth_date = models.DateField(blank=True, null=True)
    location = models.CharField(max_length=40, blank=True)
    website = models.URLField(blank=True)
    github = models.URLField(blank=True)
    linkedin = models.URLField(blank=True)
    # Set by staff, never by the owner — the serializer marks it read-only.
    is_identity_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Nothing lists profiles today, but an unordered queryset pages
        # non-deterministically the moment something does.
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.user.email
