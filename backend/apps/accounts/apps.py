from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = 'apps.accounts'

    def ready(self):
        # Importing the module connects its @receiver functions. It has to
        # happen here: in models.py it would be too early.
        import apps.accounts.signals  # noqa: F401
