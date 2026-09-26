"""Validation and normalisation for every picture the site accepts.

What reaches storage is never the file the user sent: it is re-read,
straightened, capped and re-encoded as WebP. A 4 MB phone photograph becomes
~100 KB, storage gets one format, and the EXIF — which carries GPS
coordinates — is dropped.
"""

import os
from io import BytesIO

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError
from PIL.Image import DecompressionBombError
from rest_framework import serializers

# At or below DATA_UPLOAD_MAX_MEMORY_SIZE, which refuses an oversized body
# before a view runs. That is the real defence; this gives the message.
MAX_UPLOAD_SIZE = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# What Pillow may decode. Pillow picks its decoder from the bytes, not the
# name, so without this a BMP or TIFF called photo.png sailed through — and
# an EPS made Pillow shell out to Ghostscript, which has a long history of
# remote-code-execution bugs.
ALLOWED_FORMATS = ["JPEG", "PNG", "WEBP"]

# Bytes on disk is the wrong number: a 13000x13000 flat-colour PNG is a few
# hundred kilobytes compressed and hundreds of megabytes decoded — a
# "decompression bomb" measured at 382 MB of RAM from a 308 KB upload. The
# pixel count is what must be capped, before the decode. 50 megapixels is
# ~8660x5770, larger than any phone or camera a visitor will upload.
MAX_IMAGE_PIXELS = 50_000_000

# Longest side a stored picture may have, per use. Raising these costs
# storage and load time.
AVATAR_MAX_SIDE = 512
COVER_MAX_SIDE = 1600
BODY_IMAGE_MAX_SIDE = 1600
WEBP_QUALITY = 82


def _check(file, label):
    if file.size > MAX_UPLOAD_SIZE:
        raise serializers.ValidationError(f"{label} size must not exceed 10 MB.")
    extension = os.path.splitext(file.name)[1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise serializers.ValidationError("Unsupported image format.")
    # The extension is only what the client typed; `shrink` is the real gate.


def shrink(file, max_side):
    """Return `file` straightened, no larger than `max_side`, as WebP."""
    try:
        # open() reads the header only, so the dimensions are known before a
        # single pixel is decoded — the ceiling must be checked while cheap.
        image = Image.open(file, formats=ALLOWED_FORMATS)
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise serializers.ValidationError(
                "The image dimensions are too large. Maximum 50 megapixels."
            )
        image.load()
    except DecompressionBombError:
        # Pillow's own ceiling, hit before ours on some formats. It is not an
        # OSError, so it escaped as an attacker-triggered 500.
        raise serializers.ValidationError("The image dimensions are too large.") from None
    except (UnidentifiedImageError, OSError):
        raise serializers.ValidationError("The file is not a readable image.") from None

    # Apply the orientation the phone recorded, then forget the EXIF.
    image = ImageOps.exif_transpose(image)
    # WebP keeps transparency, so only exotic modes (CMYK, palettes) convert.
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.mode else "RGB")
    image.thumbnail((max_side, max_side))

    buffer = BytesIO()
    image.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=4)

    stem = os.path.splitext(os.path.basename(file.name))[0][:60] or "image"
    return ContentFile(buffer.getvalue(), name=f"{stem}.webp")


def validate_avatar(file):
    """A profile picture. Displayed small, so the ceiling is the small one."""
    _check(file, "Avatar")
    return shrink(file, AVATAR_MAX_SIDE)


def validate_avatar_cover(file):
    """A profile banner, at the cover ceiling."""
    _check(file, "Image")
    return shrink(file, COVER_MAX_SIDE)


def validate_cover(file):
    """A wide header picture — a post's cover, a profile banner."""
    if not file:
        return file
    _check(file, "Cover")
    return shrink(file, COVER_MAX_SIDE)


def validate_body_image(file):
    """A picture inside written content — what a rich-text editor uploads."""
    _check(file, "Image")
    return shrink(file, BODY_IMAGE_MAX_SIDE)
