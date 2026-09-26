"""Put the default profile picture and cover where the model expects them.

MEDIA_ROOT is never in the repository, so without this every new account
shows a broken image. The files ship in apps/accounts/defaults/ and deploy.sh
runs this after migrate. An existing file is left alone, so a deliberately
replaced default survives.
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.accounts.models import Profile

SOURCE = Path(__file__).resolve().parents[2] / "defaults"


class Command(BaseCommand):
    help = "Copy the default avatar and cover into MEDIA_ROOT if they are missing."

    def handle(self, *args, **options):
        for field in ("avatar", "cover"):
            relative = Profile._meta.get_field(field).default
            target = Path(settings.MEDIA_ROOT) / relative
            if target.exists():
                self.stdout.write(f"present  {relative}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / Path(relative).name, target)
            self.stdout.write(self.style.SUCCESS(f"copied   {relative}"))
