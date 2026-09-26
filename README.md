# Django Starter

A production-ready base for any new Django + DRF project: the whole account
journey (sign-up, email activation, JWT sign-in, Google, password reset,
email change, profile and images), with the settings, the security, the
tests and the deployment files already in place.

Extracted from a project that ran on a real server. Every comment in the
code says *why* the line is there, because most of them were written after
something went wrong.

```bash
git clone https://github.com/MohammedAMohsen/django-drf-starter.git Shop
cd Shop && rm -rf .git
# then follow START-NEW-PROJECT.md — from the copy to a running server
```

> **A template you copy, not a library you install.** Copy it, delete
> `.git`, and build on top. Nothing here asks you to learn a custom layer —
> it is plain Django and DRF.

**What is already built:** full accounts with JWT and Google sign-in · email
activation and a confirmed email change · profiles with images that are
resized and stripped of EXIF automatically · Celery and Redis wired up ·
account lockout · rate limiting in two layers · one shape for every error ·
a health endpoint · OpenAPI docs · 124 tests · nginx, systemd, deploy and
backup files ready · CI on GitHub Actions.

**Security:** three external security reviews have been through this code.
Every finding was fixed, and each fix has a test that keeps it fixed.

---

## Where to start

| I want to… | Read |
|---|---|
| start a new project from this base | **[START-NEW-PROJECT.md](START-NEW-PROJECT.md)** |
| add my own apps on top (products, orders…) | **[BUILD-APPS.md](BUILD-APPS.md)** |
| deploy it to a server | **[DEPLOY.md](DEPLOY.md)** |
| know which value to change, and why | [VARIABLES.md](VARIABLES.md) |
| know what to add as the project grows | [SCALING.md](SCALING.md) |

---

## What is inside

```
backend/
  config/
    settings.py       one file; every environment difference lives in .env
    urls.py           the full URL map, documented at the top of the file
    celery.py         the Celery application
    middleware.py     keeps /api/ out of search results
    exceptions.py     one shape for every error the API returns
    health.py         GET /api/health/ — database and cache probe
    spa.py            serve the built front end from Django (optional)
  apps/accounts/      User · Profile · the whole account journey · bans · 124 tests
  templates/emails/   five HTML messages
deploy/               nginx · systemd · deploy.sh · backup.sh
.github/workflows/    CI: tests plus a production security check
```

## Endpoints

Everything lives under `/api/v1/`, except `health` and `schema` — both are
deliberately outside the version prefix.

| Path | What it does |
|---|---|
| `POST /api/v1/auth/users/` | register |
| `POST /api/v1/auth/users/activation/` | activate from the mailed link |
| `POST /api/v1/auth/users/resend_activation/` | resend the activation link |
| `POST /api/v1/auth/jwt/create/` | sign in → access + refresh |
| `POST /api/v1/auth/jwt/refresh/` | renew the access token |
| `POST /api/v1/auth/jwt/verify/` | check that a token is still valid |
| `POST /api/v1/auth/users/logout/` | sign out (blacklists the refresh token) |
| `POST /api/v1/auth/users/reset_password/` | ask for a reset link |
| `POST /api/v1/auth/users/reset_password_confirm/` | set a password from the link |
| `POST /api/v1/auth/users/set_password/` | change the password (asks for the current one) |
| `POST /api/v1/auth/users/set-new-password/` | set a password on a Google account |
| `POST /api/v1/auth/users/change-email/` | request an email change (**needs `current_password`**) |
| `POST /api/v1/auth/users/confirm-email-change/` | confirm the new address |
| `GET \| PUT \| PATCH /api/v1/auth/users/me/` | the current account / edit name and username |
| `DELETE /api/v1/auth/users/me/` | delete the account (needs `current_password`) |
| `POST /api/v1/google/` | sign in with a Google ID token |
| `GET /api/v1/profile/?search=` | people search (two characters minimum) |
| `GET \| PUT \| PATCH /api/v1/profile/me/` | your own profile |
| `GET /api/v1/profile/<username>/` | a public profile |
| `GET /api/health/` | service health |
| `GET /api/schema/swagger-ui/` | the API, documented and clickable |

> Djoser's `set_email` and `reset_email` routes are **removed on purpose**.
> They applied a new address immediately, with no proof that it is real —
> the exact hole `pending_email` closes. An email change goes through
> `change-email` and then `confirm-email-change`, and nothing else.

> **Google accounts and account deletion.** An account created through
> Google has no password, and `DELETE /auth/users/me/` requires
> `current_password` — so deleting it fails until a password is set. **This
> is deliberate:** the front end sends the person to `set-new-password`
> first, then back to delete. Do not loosen that requirement.

> **Banning an account.** From the admin, the *Ban* and *Lift the ban*
> actions. `is_banned` is kept separate from `is_active` on purpose:
> `is_active=False` on its own only means "has not confirmed their email",
> and a ban must not be undone by a fresh activation link or by a Google
> sign-in. Lifting a ban reactivates only accounts that had signed in before.

## API versions

When a response changes in a way that breaks existing clients: register the
new views under a second `api/<version>/` block in `config/urls.py`, and add
`"v2"` to `ALLOWED_VERSIONS` in the settings. Views that did not change serve
both versions, and `request.version` tells a view which one it is answering.

## Tests

```bash
python manage.py test
```

124 tests covering every sensitive decision in the accounts app. Run them the
moment you copy this project. To see the traceback behind a 500 during a test:

```bash
TEST_LOG=ERROR python manage.py test
```

## What is deliberately not here

No ready-made permission classes, no abstract models, no mixins. You write
`permission_classes`, `perform_create` and `created_at` yourself, the same
way you would in any Django project — the standard way, with no intermediate
layer to learn first.

Also absent: Docker, a split `settings/` package, WebSockets and advanced
SEO. Add them when you actually need them.

## Built with

Django · Django REST Framework · djoser · SimpleJWT · Celery · Redis ·
PostgreSQL · Pillow · django-axes · drf-spectacular · Sentry · gunicorn ·
nginx · systemd · ruff · pip-audit · GitHub Actions

## License

MIT — see [LICENSE](LICENSE). Copy it, change it, ship it.
