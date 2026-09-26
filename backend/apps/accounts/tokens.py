"""One-time link tokens, one generator per purpose.

Django's PasswordResetTokenGenerator says nothing about what a token is
*for*, so one generator makes every flow interchangeable: djoser shares it
between activation and password reset, and using it for email confirmation
too meant a link sent to an unverified address could be replayed against
reset_password_confirm. `key_salt` goes into the HMAC and separates them.
"""

from django.contrib.auth.tokens import PasswordResetTokenGenerator


class EmailChangeTokenGenerator(PasswordResetTokenGenerator):
    """For confirming a new email address, and nothing else."""

    # Changing this invalidates every outstanding confirmation link.
    key_salt = "apps.accounts.tokens.EmailChangeTokenGenerator"

    def _make_hash_value(self, user, timestamp):
        # Ties the token to the address it was issued for: asking for a
        # different address kills the older link.
        return f"{super()._make_hash_value(user, timestamp)}{user.pending_email}"


class ActivationTokenGenerator(PasswordResetTokenGenerator):
    """For confirming a new account, and nothing else."""

    key_salt = "apps.accounts.tokens.ActivationTokenGenerator"


activation_token = ActivationTokenGenerator()
email_change_token = EmailChangeTokenGenerator()
