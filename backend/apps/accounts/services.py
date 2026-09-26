"""Logic a view calls, and a task or command could call too."""

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.text import slugify
from rest_framework import serializers

from .models import User
from .tasks import send_change_email, send_email_change_notice
from .validators.user import RESERVED_USERNAMES

NUMBERED_USERNAME_ATTEMPTS = 5


def _acceptable_base(email):
    """A starting username that obeys the rules in validators/user.py.

    A reserved or too-short base is suffixed rather than replaced, so the
    handle still resembles the address it came from.
    """
    base = slugify(email.split("@")[0], allow_unicode=True)[:24]
    # slugify produces hyphens; the username rules allow underscores.
    base = base.replace("-", "_").strip("_")

    if not base:
        return "user"
    # "_user", not a bare "_": a trailing underscore is invalid too.
    if base.lower() in RESERVED_USERNAMES or len(base) < 3:
        return f"{base}_user"
    return base


def username_candidates(email):
    """Yield usernames to try for `email`, best first.

    The plain name, five numbered ones, then one with five random digits.
    allow_unicode matters: without it a non-ASCII local part such as
    محمد@example.com slugifies to "".
    """
    base = _acceptable_base(email)
    yield base
    for counter in range(1, NUMBERED_USERNAME_ATTEMPTS):
        yield f"{base}{counter}"
    yield f"{base}{get_random_string(5, '0123456789')}"


def create_user_with_unique_username(email, create=None, **fields):
    """Create a user, retrying the username if another signup wins the race.

    atomic() is required, not decoration: inside an outer transaction a
    failed statement poisons it, and the savepoint is what makes "try the
    next name" possible.

    `create` is the manager method that inserts. The default suits the
    Google path (no password at all); the signup path must pass
    `User.objects.create_user`, which hashes the password.
    """
    create = create or User.objects.create
    for candidate in username_candidates(email):
        if User.objects.filter(username=candidate).exists():
            continue
        try:
            with transaction.atomic():
                return create(email=email, username=candidate, **fields)
        except IntegrityError:
            # Only swallow the collision we expected; a duplicate email must
            # not be hidden.
            if not User.objects.filter(username=candidate).exists():
                raise
    raise IntegrityError(f"Could not allocate a username for {email!r}.")


def change_user_email(user, pending_email):
    """Park the new address, then send two emails.

    To the NEW address: a confirmation link — nothing changes until it is
    opened. To the OLD address: a warning. A borrowed session can confirm an
    address of its own, but it cannot stop the owner being told.
    """
    old_email = user.email
    user.pending_email = pending_email
    user.pending_email_set_at = timezone.now()
    user.save(update_fields=["pending_email", "pending_email_set_at"])
    # Queued only once the row is committed: a worker must not read a stale
    # row, and a broker outage must not 500 a request that already wrote.
    transaction.on_commit(lambda: send_change_email.delay(user.pk))
    transaction.on_commit(
        lambda: send_email_change_notice.delay(user.pk, old_email, pending_email)
    )


def revoke_all_tokens(user):
    """End every session this user currently has.

    A password change invalidates tokens on its own (CHECK_REVOKE_TOKEN); a
    change of address does not touch the password, so it needs this.
    """
    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken,
        OutstandingToken,
    )

    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


def confirm_user_email(user):
    """Apply a pending address, if it is still free.

    Checked again here because days can pass between asking and clicking.
    """
    if User.objects.filter(email=user.pending_email).exclude(pk=user.pk).exists():
        raise serializers.ValidationError("Email already exists.")
    user.email = user.pending_email
    user.pending_email = None
    user.pending_email_set_at = None
    user.save(update_fields=["email", "pending_email", "pending_email_set_at"])
    revoke_all_tokens(user)
