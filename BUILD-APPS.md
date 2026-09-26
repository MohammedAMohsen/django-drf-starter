# Building your apps on top of the base

The base gives you accounts, settings and security. This file shows how to
add your own apps — with one complete, working example: a **products** app
with images.

There is nothing specific to this base that you need to learn. You write
Django and DRF as usual.

---

## Part one: adding a new app

### Step 1 — create it

```bash
python manage.py startapp products apps/products
```

The second argument is a path, and Django creates the folder itself.

### Step 2 — three lines that connect it to the project

Forgetting any of them gives you an app that does not work, with no clear
error message.

**a. In `apps/products/apps.py`** — prefix the name with `apps.`:

```python
class ProductsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.products'        # not 'products'
```

**b. In `config/settings.py`** — under `# Local apps`:

```python
INSTALLED_APPS = [
    ...
    'apps.accounts',
    'apps.products',        # ← here
]
```

**c. In `config/urls.py`** — inside `api_patterns` specifically:

```python
api_patterns = [
    path("auth/", include(auth_patterns)),
    path("", include("apps.accounts.urls")),
    path("", include("apps.products.urls")),      # ← here
]
```

> Why inside `api_patterns` and not `urlpatterns`? Because `api_patterns` is
> what gets mounted under `/api/<version>/`. Putting it in `urlpatterns`
> gives you `/products/` with no version number.

The result: `/api/v1/products/`

---

### Step 3 — write the app

Four files. This code has been run and works as it stands.

#### `apps/products/models.py`

```python
from django.conf import settings
from django.db import models


class Product(models.Model):
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL,          # not User directly
        on_delete=models.CASCADE,
        related_name="products",           # user.products.all()
    )
    name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    image = models.ImageField(upload_to="products/", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name
```

Three points worth noticing:

- **`settings.AUTH_USER_MODEL`**, not `from apps.accounts.models import User`.
  The direct import causes a circular import in a lot of projects.
- **`ordering`** is not decoration. Without it the same row can appear on two
  different pages while paging, because PostgreSQL guarantees no order
  without `ORDER BY`.
- **`DecimalField` for prices**, not `FloatField`. Floating point loses
  precision, and `0.1 + 0.2` does not equal `0.3`.

#### `apps/products/serializers.py`

```python
from rest_framework import serializers

from apps.accounts.validators.image import validate_cover

from .models import Product


class ProductSerializer(serializers.ModelSerializer):
    # The seller's name instead of their id, read-only.
    seller = serializers.CharField(source="seller.username", read_only=True)

    class Meta:
        model = Product
        fields = ("id", "name", "price", "image", "seller", "created_at")

    def validate_image(self, value):
        # Reuses the base's validator: refuses anything that is not an image,
        # shrinks it, and strips the EXIF. Entirely optional — drop the line
        # if you do not want it.
        return validate_cover(value)
```

#### `apps/products/views.py`

```python
from rest_framework import viewsets
from rest_framework.permissions import (
    SAFE_METHODS,
    BasePermission,
    IsAuthenticatedOrReadOnly,
)

from .models import Product
from .serializers import ProductSerializer


class IsSellerOrReadOnly(BasePermission):
    """Anyone may read; only the seller may edit or delete."""

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:      # GET · HEAD · OPTIONS
            return True
        return obj.seller == request.user


class ProductViewSet(viewsets.ModelViewSet):
    # select_related because the serializer reads seller.username — without
    # it that is one extra query per row in the list.
    queryset = Product.objects.select_related("seller")
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticatedOrReadOnly, IsSellerOrReadOnly]

    def perform_create(self, serializer):
        # The owner comes from the token, never from the request body.
        # Without this, anyone can create a product in someone else's name.
        serializer.save(seller=self.request.user)
```

> **A trap: every `@action` you write must accept `**kwargs`.**
> The version number (`v1`) reaches the view as a keyword argument from the
> URL, so a method declared like this:
> ```python
> @action(detail=False, methods=["GET"], url_path="mine")
> def mine(self, request):                 # ✗ TypeError on every call
> ```
> fails with `got an unexpected keyword argument 'version'`. The correct
> form:
> ```python
> def mine(self, request, **kwargs):       # ✓
> ```
> This applies to custom actions only — `list`, `retrieve` and the rest are
> handled by DRF. See `apps/accounts/views.py` for examples.

> **Two permission classes, each with a different job:**
> `IsAuthenticatedOrReadOnly` runs on **every request** — it stops an
> anonymous visitor creating anything. `IsSellerOrReadOnly` runs on an
> **existing object** only — it stops one user editing another's product.
>
> `has_object_permission` is **never called** on create (there is no object
> yet) and not on lists. That is why you need both.

#### `apps/products/urls.py`

```python
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("products", views.ProductViewSet, basename="product")
urlpatterns = router.urls
```

### Step 4 — the migrations

```bash
python manage.py makemigrations
python manage.py migrate
```

Repeat both after **any** change to `models.py`.

### Step 5 — try it

```bash
python manage.py runserver
```

Open `http://127.0.0.1:8000/api/schema/swagger-ui/` — `/api/v1/products/`
appears on its own, ready to try.

The URLs that were generated:

| Method | URL | Who can |
|---|---|---|
| GET | `/api/v1/products/` | everyone |
| POST | `/api/v1/products/` | signed-in users |
| GET | `/api/v1/products/<id>/` | everyone |
| PATCH/DELETE | `/api/v1/products/<id>/` | the seller only |

---

## Part two: images

### In development — do nothing

`ImageField(upload_to="products/")` and that is all. Django creates the
folders on the first upload, and `media/` is already in `.gitignore`.

The result of an actual upload through the code above:

```
uploaded:  big-photo.jpg   3000×2000   92 KB
stored:    products/big-photo.webp   1600×1067   3 KB
URL:       http://127.0.0.1:8000/media/products/big-photo.webp   → 200
```

The resizing, the WebP conversion and the EXIF removal (EXIF carries GPS
coordinates) all happened because of the `validate_cover` line in the
serializer.

> **Two ceilings, not one:** the size (10 MB) **and the dimensions** (50
> megapixels). The second is the one that matters: a flat-colour PNG at
> 13000×13000 is 300 KB on disk but costs **382 MB** once decoded. On a 2 GB
> server, two concurrent uploads kill it.

### In production — three things, once

**The whole rule: the path in `.env` and the path in nginx must match.**

**1. In `.env` on the server:**

```ini
MEDIA_ROOT=/srv/shop/media
```

**2. Create the folder and give it to the service user:**

```bash
sudo mkdir -p /srv/shop/media
sudo chown shop:shop /srv/shop/media
```

**3. Make sure nginx points at the same path** (in `deploy/nginx/shop.conf`):

```nginx
location /media/ {
    alias /srv/shop/media/;
}
```

Done. Django writes there, nginx reads from there.

### Why outside the code folder?

| | Code | User images |
|---|---|---|
| Replaced on every deploy | yes | **must not be** |
| Can be regenerated | yes (`git clone`) | **impossible** |

Three dangers when the images live inside the code folder: `git clean -fd`
(a command people type to tidy a messy tree) deletes them for good; a
clone-into-a-new-folder deploy leaves them behind; and one missing line in
`.gitignore` commits them to the repository.

> **For accuracy:** `git pull` and `git reset --hard` do **not** delete
> untracked files. `git clean -fd` is the one that does.

### An important limit: validation runs through the API only

The resizing and the EXIF removal happen in `validate_image` inside the
**serializer**. Any other route skips it:

| Route | What gets stored |
|---|---|
| The API (`POST /api/v1/…`) | resized · WebP · no EXIF ✓ |
| **The admin at `/admin/`** | the file as it is — **with GPS coordinates** |
| `loaddata`, a script, or `bulk_create` | the file as it is |

Acceptable when the admin is yours and your team's. It becomes a leak when a
staff member uploads a **user's** picture from the admin and it is published.

To normalise from every route, move it into the model:

```python
from apps.accounts.validators import shrink

class Product(models.Model):
    ...
    def save(self, *args, **kwargs):
        if self.image and not self.image.name.endswith(".webp"):
            self.image = shrink(self.image, 1600)
        super().save(*args, **kwargs)
```

The price: logic inside `save()` that runs on every save, and is awkward to
switch off in tests.

### Another limit: an account with no profile

`signals.py` creates a profile with every user — but a signal only fires on
`save()`. Users inserted by `bulk_create`, `loaddata` (restoring a backup),
`queryset.update()` or raw SQL have **no profile**.

The API handles it now (`get_or_create` in `/profile/me/`); to fix the rows
that already exist:

```bash
python manage.py backfill_profiles --dry-run   # what would happen
python manage.py backfill_profiles             # do it
```

Run it after any bulk import.

### If you forgot something — how you find out

| Symptom | Cause |
|---|---|
| Upload returns 201 but the image 404s | the path in `.env` ≠ the path in nginx |
| `PermissionError` on upload | the folder does not belong to the service user (`chown`) |
| The images vanished after a deploy | `MEDIA_ROOT` is inside the code folder |
| 413 on a large image | `client_max_body_size` in nginx is too small |

---

## Part three: things to watch

### If your project shows profiles at `/<username>`

Add every new top-level route to `RESERVED_USERNAMES` in
`apps/accounts/validators/user.py`:

```python
RESERVED_USERNAMES = {
    ...
    "cart", "checkout", "orders", "products",
}
```

Without it, the first person to register as `cart` takes the shopping-cart
page for good.

### Write a test for every decision

Look at `apps/accounts/tests.py` as a model. Three questions deserve a test
in every app:

```python
# 1. Can a non-owner edit it?              (must be 403)
# 2. Can an anonymous visitor create it?   (must be 401)
# 3. Does a private field appear in the public response?  (must not)
```

Then `python manage.py test`.

### A background task

```python
# apps/products/tasks.py
from celery import shared_task

@shared_task
def rebuild_index(product_id):
    ...
```

Call it with `.delay(pk)` — and pass the key, never the object. From a view
that writes to the database:

```python
from django.db import transaction
transaction.on_commit(lambda: rebuild_index.delay(product.pk))
```

Without `on_commit` the worker may pick the job up before the transaction is
committed and read a stale row.

### A periodic task

In `config/settings.py`. The `from celery.schedules import crontab` import is
already at the top of the file, and `CELERY_BEAT_SCHEDULE` is **not empty** —
it holds two jobs that really run. Add yours to it; do not replace it:

```python
CELERY_BEAT_SCHEDULE = {
    'clear-stale-pending-emails': {...},   # already there — keep it
    'flush-expired-tokens': {...},         # already there — keep it
    "nightly-cleanup": {                   # ← yours
        "task": "apps.products.tasks.cleanup",
        "schedule": crontab(hour=4, minute=0),
    },
}
```

The scheduler runs inside the worker with `-B`, which is right for exactly
one worker — two would run every periodic task twice (see
[SCALING.md](SCALING.md), item 10).

### The admin

```python
# apps/products/admin.py
from django.contrib import admin
from .models import Product

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "seller", "created_at")
    search_fields = ("name",)
    raw_id_fields = ("seller",)     # important: without it every user is loaded into a <select>
```

---

## Summary

```bash
python manage.py startapp products apps/products
# 1. apps.py      → name = 'apps.products'
# 2. settings.py  → INSTALLED_APPS
# 3. urls.py      → api_patterns
# 4. write models · serializers · views · urls
python manage.py makemigrations && python manage.py migrate
python manage.py test
```

To deploy: [DEPLOY.md](DEPLOY.md)
