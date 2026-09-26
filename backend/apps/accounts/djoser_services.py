"""Make djoser's emails go through Celery.

Djoser sends its activation and reset mail inside the request, so a signup
waits on an SMTP handshake. These replace its `send` with a queue push;
settings.DJOSER["EMAIL"] points at them. Only the delivery moves — djoser
still builds the context, and the message text is ours (emails.py).

`transaction.on_commit` because a worker can otherwise pick the job up before
the row is committed, find no user, and die in the background — so the person
waits for mail that never comes. Djoser happens to send after its own commit
today, but that is timing we do not control.
"""

from django.db import transaction
from djoser.email import ActivationEmail, PasswordChangedConfirmationEmail, PasswordResetEmail

from .tasks import (
    send_activation_email,
    send_password_changed_email,
    send_reset_password_email,
)


def _queue(task, user):
    # The pk is read now and captured by the closure — never the object; see
    # tasks.py.
    user_id = user.pk
    transaction.on_commit(lambda: task.delay(user_id))


class CeleryActivationEmail(ActivationEmail):
    def send(self, to, *args, **kwargs):
        _queue(send_activation_email, self.context["user"])


class CeleryPasswordResetEmail(PasswordResetEmail):
    def send(self, to, *args, **kwargs):
        _queue(send_reset_password_email, self.context["user"])


class CeleryPasswordChangedEmail(PasswordChangedConfirmationEmail):
    def send(self, to, *args, **kwargs):
        _queue(send_password_changed_email, self.context["user"])
