"""Sending mail, off the request.

Queued rather than inline because an SMTP connection can take seconds or
hang, and the person who pressed "sign up" is waiting on it. A mail server
having a bad minute must not turn a successful signup into a 500.

The argument is always a primary key, never the user object: task arguments
sit serialised in Redis, so an object would ship a row that may be stale.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from .emails import (
    activation_message,
    change_email_message,
    email_change_requested_message,
    password_changed_message,
    reset_password_message,
)
from .models import User

logger = logging.getLogger(__name__)

MAIL_TASK = {
    "autoretry_for": (Exception,),
    # A deleted account is not a transient failure: retrying holds a worker
    # and reports the same non-problem to Sentry three times.
    "dont_autoretry_for": (User.DoesNotExist,),
    "retry_backoff": 10,  # 10s, then 20s, then 40s
    "retry_kwargs": {"max_retries": 3},
    "retry_jitter": True,
}


def _send(subject, to, text, html):
    """Text as the body, HTML as an alternative — a client picks one."""
    message = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [to])
    message.attach_alternative(html, "text/html")
    message.send(fail_silently=False)


@shared_task(**MAIL_TASK)
def send_activation_email(user_id):
    user = User.objects.get(pk=user_id)
    _send("Activate your account", user.email, *activation_message(user))


@shared_task(**MAIL_TASK)
def send_reset_password_email(user_id):
    user = User.objects.get(pk=user_id)
    _send("Reset your password", user.email, *reset_password_message(user))


@shared_task(**MAIL_TASK)
def send_password_changed_email(user_id):
    user = User.objects.get(pk=user_id)
    _send("Your password was changed", user.email, *password_changed_message(user))


@shared_task(**MAIL_TASK)
def send_email_change_notice(user_id, old_email, new_email):
    """Warn the old address that a change was requested.

    `old_email` is passed in, not read from the row: the change may already
    be confirmed by the time a worker runs this, and `user.email` would then
    send the warning to whoever asked for the change.
    """
    user = User.objects.get(pk=user_id)
    _send(
        "Someone asked to change your email address",
        old_email,
        *email_change_requested_message(user, new_email),
    )


@shared_task(**MAIL_TASK)
def send_change_email(user_id):
    user = User.objects.get(pk=user_id)
    if not user.pending_email:
        # Confirmed or cancelled while this job sat in the queue. No address
        # left to write to, and sending to None raises.
        logger.info("Email change for user %s was already resolved; nothing to send.", user_id)
        return
    # Sent to the new address: proving control of it is the entire point.
    _send("Confirm your new email address", user.pending_email, *change_email_message(user))


@shared_task
def clear_stale_pending_emails():
    """Release addresses reserved by a request nobody ever confirmed.

    The link dies after PASSWORD_RESET_TIMEOUT, but that is not clearing the
    reservation: ChangeEmailSerializer refuses any address pending for
    anybody, so an abandoned request held it out of reach permanently.

    Run daily — see CELERY_BEAT_SCHEDULE.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT)
    cleared = User.objects.filter(
        pending_email__isnull=False, pending_email_set_at__lt=cutoff
    ).update(pending_email=None, pending_email_set_at=None)
    if cleared:
        logger.info("Cleared %s stale pending email change(s).", cleared)
    return cleared


@shared_task
def flush_expired_tokens():
    """Delete refresh tokens that have already expired.

    With rotation and blacklisting every renewal writes two rows — roughly
    17,000 a year for an active user — and nothing was removing them. An
    expired token's signature no longer validates, so deleting it takes
    nothing away. SimpleJWT ships the command; this only schedules it.
    """
    from django.core.management import call_command

    call_command("flushexpiredtokens", verbosity=0)
    logger.info("Flushed expired refresh tokens.")
