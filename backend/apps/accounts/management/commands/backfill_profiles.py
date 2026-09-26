"""Create the missing Profile rows.

signals.py gives every user a profile, but a signal fires on save() only — a
row inserted by bulk_create, loaddata, queryset.update() or raw SQL has none.
Run this after any bulk import; the API also creates one on demand.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Profile, User


class Command(BaseCommand):
    help = "Create a Profile for every user that has none."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what is missing without writing anything.",
        )

    def handle(self, *args, **options):
        missing = User.objects.filter(profile__isnull=True).order_by("pk")
        count = missing.count()

        if not count:
            self.stdout.write(self.style.SUCCESS("Every user already has a profile."))
            return

        if options["dry_run"]:
            for user in missing:
                self.stdout.write(f"would create  {user.pk}  {user.email}")
            self.stdout.write(self.style.WARNING(f"{count} missing (dry run, nothing written)"))
            return

        # bulk_create skips post_save, which is what left these rows without
        # a profile — safe here only because this command creates them.
        with transaction.atomic():
            Profile.objects.bulk_create([Profile(user=user) for user in missing])

        self.stdout.write(self.style.SUCCESS(f"created {count} profile(s)"))
