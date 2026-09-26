"""What must happen the moment a user row is created.

A signal rather than an override of User.save(), because users are created
from four places — signup, Google sign-in, createsuperuser and the tests —
and all four must end up with a profile.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile, User


@receiver(post_save, sender=User)
def create_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)
        # A good place for the project's own welcome work:
        # transaction.on_commit(lambda: send_welcome_email.delay(instance.pk))
