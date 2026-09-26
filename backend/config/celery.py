"""The Celery application.

Imported by config/__init__.py so it exists the moment Django starts: that is
what attaches every @shared_task to it and what lets `celery -A config
worker` find it.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")

# Every CELERY_* name in settings.py becomes a Celery setting.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Finds tasks.py in each installed app.
app.autodiscover_tasks()
