"""Rules for what a name, a username and an email address may be.

Plain functions, not DRF validator classes, so a serializer, a service or a
management command can all call them. They raise DRF's ValidationError,
which becomes a 400 on the right field. (admin.py translates it for forms.)

Arabic is allowed alongside English — drop the ء-ي range for an
English-only audience.
"""

import re

from rest_framework import serializers

# A username appears in a URL as /<username>. Update this with every new
# top-level route, or whoever registers "settings" takes that page.
RESERVED_USERNAMES = {
    # roles
    "admin", "administrator", "root", "superuser", "system", "support",
    "help", "contact", "security", "abuse", "noreply", "no-reply",
    # server-owned paths
    "api", "auth", "static", "media", "assets", "silk",
    # application pages — prune or extend per project
    "login", "signup", "logout", "register", "search", "explore",
    "settings", "profile", "account", "notifications", "about", "privacy",
    "terms", "activate", "reset-password", "confirm-email",
}

# Starts with a letter, ends with a letter or digit. Anchored at both ends —
# an unanchored pattern matches a good username buried inside a bad one.
USERNAME_PATTERN = re.compile(r"^[a-zA-Zء-ي][a-zA-Z0-9ء-ي_]*[a-zA-Z0-9ء-ي]$")

# `*` not `?`: allowing one separator refused ordinary names such as
# "محمد علي حسن" or "Abd Allah Al-Sayed", and a name is not a security
# boundary. Still refused: digits, symbols, emoji, markup, a leading or
# trailing separator, and two separators in a row.
NAME_PATTERN = re.compile(r"^[a-zA-Zء-ي]+(?:[ '\-][a-zA-Zء-ي]+)*$")

# Two, not three. "Li", "Bo" and "Jo" are whole names for a lot of people.
MIN_NAME_LENGTH = 2
# A ceiling so a name cannot be a paragraph. Matches the model's max_length.
MAX_NAME_LENGTH = 150

# A signup from one of these is an account nobody can ever recover, and
# usually a spam account.
BLOCKED_EMAIL_DOMAINS = {
    "mailinator.com",
    "10minutemail.com",
    "tempmail.com",
    "guerrillamail.com",
    "yopmail.com",
    "throwawaymail.com",
}


def validate_name(value):
    value = value.strip()
    if len(value) < MIN_NAME_LENGTH:
        raise serializers.ValidationError(
            f"The name must be at least {MIN_NAME_LENGTH} letters long."
        )
    if len(value) > MAX_NAME_LENGTH:
        raise serializers.ValidationError(
            f"The name must be at most {MAX_NAME_LENGTH} characters long."
        )
    if not NAME_PATTERN.match(value):
        raise serializers.ValidationError(
            "Letters only, separated by a single space, hyphen or apostrophe."
        )
    return value


def validate_username(value):
    # Each rule reports its own reason; one combined "invalid username" makes
    # the person retry blind.
    if not (3 <= len(value) <= 20):
        raise serializers.ValidationError("Username must be between 3 and 20 characters.")
    if not re.match(r"^[a-zA-Zء-ي]", value):
        raise serializers.ValidationError("Username must start with a letter.")
    if not re.match(r"^[a-zA-Z0-9ء-ي_]+$", value):
        raise serializers.ValidationError(
            "Username can only contain letters, numbers, and underscores (_)."
        )
    if "__" in value:
        raise serializers.ValidationError("Username cannot contain consecutive underscores (__).")
    if value.endswith("_"):
        raise serializers.ValidationError("Username cannot end with an underscore (_).")
    if value.lower() in RESERVED_USERNAMES:
        raise serializers.ValidationError("This username is reserved.")
    # Lowercased so "Ahmed" and "ahmed" can never be two accounts.
    return value.lower()


def validate_email(value):
    domain = value.split("@")[-1].lower()
    if domain in BLOCKED_EMAIL_DOMAINS:
        raise serializers.ValidationError("Temporary email addresses are not allowed.")
    # The address is the sign-in credential and a unique index is
    # case-sensitive.
    return value.lower()
