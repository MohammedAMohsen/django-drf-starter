"""The links and the words in every account email.

Separate from tasks.py so what a message says can change without touching
how it is sent. Every link points at the front end, never at the API: the
person lands on a page, and that page posts the uid and token.

The token needs no storage — it is a signed hash of the user's pk, password
hash, last_login and a timestamp, so it expires on its own
(PASSWORD_RESET_TIMEOUT) and dies the moment the password changes.

Each message goes out as text and HTML: clients that cannot render HTML, and
anything reading the mail aloud, need the text, and HTML alone reads as spam.
"""

from django.conf import settings
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .tokens import activation_token, email_change_token

# `confirm_email` has its own generator so its tokens cannot be replayed
# against password reset — see tokens.py.
PAGE_TOKEN_GENERATORS = {
    "activate": activation_token,
    "confirm_email": email_change_token,
}


def build_user_token(user, page=None):
    """The (uid, token) pair that identifies a user in a one-time link."""
    generator = PAGE_TOKEN_GENERATORS.get(page) or PasswordResetTokenGenerator()
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    return uid, generator.make_token(user)


def build_link(user, page):
    """A one-time link to a page on the front end.

    `page` is a key of settings.FRONTEND_PATHS — the same dict djoser reads,
    so each page has one definition rather than two that can drift apart.
    """
    uid, token = build_user_token(user, page)
    path = settings.FRONTEND_PATHS[page].format(uid=uid, token=token)
    return f"{settings.EMAIL_FRONTEND_PROTOCOL}://{settings.DOMAIN}/{path}"


def _render(template, *, link="", button="", body, name="", **extra):
    """Return (text, html) for one message.

    The text version is assembled here rather than in a second template, so
    both versions live in one call and cannot drift apart.
    """
    text = (
        f"{body}\n\n"
        + (f"{link}\n\n" if link else "")
        + "If you did not expect this email, you can ignore it.\n\n"
        f"The {settings.SITE_NAME} team\n"
    )
    html = render_to_string(
        f"emails/{template}.html",
        {
            "site_name": settings.SITE_NAME,
            "link": link,
            "button": button,
            "name": name,
            **extra,
        },
    )
    return text, html


def activation_message(user):
    link = build_link(user, "activate")
    return _render(
        "activation",
        link=link,
        button="Activate my account",
        name=user.first_name,
        body=f"An account was created for this address on {settings.SITE_NAME}. "
             f"Open the link below to confirm it.",
    )


def reset_password_message(user):
    link = build_link(user, "reset_password")
    return _render(
        "reset_password",
        link=link,
        button="Set a new password",
        body=f"A password reset was requested for your {settings.SITE_NAME} account. "
             f"Open the link below to choose a new one.",
    )


def password_changed_message(user):
    # No link: a notice, not an action. Its job is to reach the owner if
    # somebody else changed their password.
    return _render(
        "password_changed",
        body=f"The password on your {settings.SITE_NAME} account was just changed. "
             f"If that was not you, reset it immediately and contact support.",
    )


def email_change_requested_message(user, new_email):
    """Sent to the CURRENT address when a change is requested.

    `pending_email` cannot help the owner when a borrowed session confirms an
    address of its own — that link goes to the attacker. Only a message to
    the old address tells the owner a change was asked for.

    No link: an action here is one more thing an attacker could aim at. The
    instruction is to change the password, which kills every token.
    """
    return _render(
        "email_change_requested",
        new_email=new_email,
        body=f"Someone asked to change the email address on your "
             f"{settings.SITE_NAME} account to {new_email}. If that was not "
             f"you, change your password immediately and contact support.",
    )


def change_email_message(user):
    link = build_link(user, "confirm_email")
    return _render(
        "change_email",
        link=link,
        button="Confirm this address",
        body=f"You asked to change the address on your {settings.SITE_NAME} account "
             f"to this one. Open the link below to confirm it.",
    )
