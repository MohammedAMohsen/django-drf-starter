# What to change when you start a new project

A quick reference for every value. The guides, in order:
[START-NEW-PROJECT.md](START-NEW-PROJECT.md) to copy ·
[BUILD-APPS.md](BUILD-APPS.md) to build your apps ·
[DEPLOY.md](DEPLOY.md) to deploy.

Every example here is for an imaginary project called **Shop** on the domain
**myshop.com**.

---

## 1. Required — in `backend/.env`

| Variable | Current value | Set it to, for example | Why |
|---|---|---|---|
| `SECRET_KEY` | `change-me-...` | `Xk9mQ...` (50 random characters) | Signs tokens and sessions. One key shared by two projects means breaking into one opens the other |
| `SITE_NAME` | `MyProject` | `Shop` | Appears in the text of every email and in the API docs title |
| `DB_NAME` | `myproject_db` | `shop_db` | The database name |
| `DB_USER` | `myproject` | your user locally · `shop` on the server | The PostgreSQL user |
| `DB_PASSWORD` | `change-me` | empty locally · a strong one on the server | Local `peer auth` asks for no password |
| `FRONTEND_URL` | `localhost:5173` | `localhost:5173` locally · `myshop.com` on the server | **No `http://`** — every mailed link is built from it |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | `myshop.com,www.myshop.com` | With `DEBUG=False` the project **refuses to start** if this is empty |

Generate the key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

---

## 2. Important when you deploy

| Variable | Current value | Set it to, for example | Why |
|---|---|---|---|
| `DEBUG` | `True` | `False` on the server | `True` on a real server shows any visitor your code and your settings at the first error |
| `ADMIN_URL` | `admin/` | `shop-panel-7x2k/` | `/admin/` is the first path every scanner tries. **Changed it here? Change it in `deploy/nginx/*.conf` too — otherwise the request never reaches Django and the panel disappears with no error** |
| `CSRF_TRUSTED_ORIGINS` | empty | `https://myshop.com,https://www.myshop.com` | **With** `https://` here, unlike `FRONTEND_URL` |
| `MEDIA_ROOT` | unset (= `backend/media/`) | `/srv/shop/media` | Code is replaceable, uploads are not. Read the note below the table |
| `CELERY_ALWAYS_EAGER` | `False` | `True` locally · `False` on the server | Locally: tasks run immediately, with no Redis and no worker |
| `DEFAULT_FROM_EMAIL` | empty | `Shop <hello@myshop.com>` | The visible sender address |

---

## 3. In the code

| File | What to change | Example for Shop |
|---|---|---|
| `apps/accounts/validators/user.py` | `RESERVED_USERNAMES` — **the most important one** | add `cart`, `checkout`, `orders`, `products`, `payment` |
| `apps/accounts/models.py` | the `Profile` fields | drop `github`/`linkedin`, add `phone`/`address` |
| `apps/accounts/emails.py` | the wording of the five messages (activation · reset · password changed · email change requested · confirm the new address) | they are generic now; match your own tone |
| `config/settings.py` | `INSTALLED_APPS` | add `'apps.products'` under `# Local apps` |
| `config/urls.py` | `api_patterns` | add `path("", include("apps.products.urls"))` inside the list |
| `templates/emails/` | the wording and the look | change the colour `#111827` and the language |
| `config/settings.py` | `CELERY_BEAT_SCHEDULE` | it holds two jobs that really run (clearing stale pending emails · flushing expired tokens). **Add to it, do not replace it** |

> **Why keep `MEDIA_ROOT` outside the code folder?** On a server the project
> folder is a checkout of a git repository, replaced and rebuilt on every
> deploy. User uploads are not: they cannot be regenerated. Three real
> dangers when they live inside it: `git clean -fd` (a command people type to
> tidy a messy tree) deletes them for good; a clone-into-a-new-folder deploy
> leaves them behind; and one missing line in `.gitignore` commits them to
> the repository. Outside the folder none of that is possible, and the backup
> targets a fixed path that no deploy moves.
>
> (For accuracy: `git pull` and `git reset --hard` do **not** delete
> untracked files. `git clean -fd` is the one that does.)

> **Why is `RESERVED_USERNAMES` the most important?** A username appears as
> the link `myshop.com/<username>`. If somebody registers as `cart`, the
> shopping-cart link opens their profile — and you lose that page for good.

### Renaming the deployment files (full steps in [DEPLOY.md](DEPLOY.md))

```bash
cd deploy && grep -rl 'myproject\|example\.com' . | xargs sed -i 's/myproject/shop/g; s/example\.com/myshop.com/g' && mv nginx/myproject.conf nginx/shop.conf && for f in systemd/myproject-*; do mv "$f" "${f/myproject/shop}"; done
```

---

## 4. Optional

| Variable | When to change it |
|---|---|
| `GOOGLE_CLIENT_ID` | Only if you want Google sign-in. Leave it empty and the endpoint refuses every token — which is correct behaviour |
| `AXES_ENABLED` | **`True` when you deploy.** Locks the **account** (not the address) after 5 failed attempts for an hour. Leave it `False` locally so a forgotten password does not lock you out of your own machine |
| `NUM_PROXIES` | In `settings.py`, not `.env`. **It stays `1`** — even behind Cloudflare. Nginx *overwrites* the header with `$remote_addr`, so there is only ever one entry. Behind a CDN the fix belongs in nginx: `real_ip_header CF-Connecting-IP` with `set_real_ip_from` for the CDN's ranges. Raising the number gives every visitor in the world one shared counter |
| `HSTS_INCLUDE_SUBDOMAINS` · `HSTS_PRELOAD` | `True` by default. Set them `False` if your domain has subdomains you cannot serve over HTTPS — the rule is enforced for a year and cannot be undone quickly |
| `SENTRY_DSN` | **Turn it on when you deploy.** Empty sends nothing; with a DSN from sentry.io the errors reach you with their full traceback instead of waiting for a user to complain |
| `SENTRY_ENVIRONMENT` | `production` or `staging` — to tell servers apart in the Sentry dashboard |
| `SENTRY_TRACES_SAMPLE_RATE` | The fraction of requests recorded for timing. `1.0` until traffic is real, then lower it |
| `EMAIL_HOST` / `EMAIL_USER` / `EMAIL_PASS` | Leave them empty locally: mail is printed to the terminal instead of sent |
| `SERVE_SPA` | `True` only if a front end is served from the same domain |
| `LOG_LEVEL` | `DEBUG` while chasing a problem, `INFO` otherwise |

---

## 5. Values in the code you may need to tune

| Where | Value | Default | When to change it |
|---|---|---|---|
| `settings.py` → `SIMPLE_JWT` | `ACCESS_TOKEN_LIFETIME` | 15 minutes | The window in which a token that should be dead still works (after a ban, a sign-out). Rotation means an active session never notices. **Do not lengthen it** |
| `settings.py` → `SIMPLE_JWT` | `REFRESH_TOKEN_LIFETIME` | 30 days | How long someone may stay away before being asked to sign in again |
| `settings.py` → `DEFAULT_THROTTLE_RATES` | `user` | 300/min | Raise it if the front end polls heavily |
| " | `auth` | 10/min | **Do not raise it.** This is what stops password guessing |
| `settings.py` → `REST_FRAMEWORK` | `PAGE_SIZE` | 10 | 20 for a product listing |
| `settings.py` → `REST_FRAMEWORK` | `ALLOWED_VERSIONS` | `['v1']` | Add `'v2'` when you ship a second version |
| `validators/image.py` | `MAX_UPLOAD_SIZE` | 10 MB | Must stay ≤ `DATA_UPLOAD_MAX_MEMORY_SIZE` (12 MB) |
| `validators/image.py` | `AVATAR_MAX_SIDE` / `COVER_MAX_SIDE` | 512 / 1600 px | Raising them costs storage and load time |
| `validators/user.py` | `BLOCKED_EMAIL_DOMAINS` | 6 throwaway domains | Add whichever ones bother you |
| `validators/image.py` | `MAX_IMAGE_PIXELS` | 50 megapixels | A ceiling on dimensions, not on bytes — a 300 KB image can cost 382 MB of RAM |
| `models.py` | `BIO_MAX_LENGTH` | 500 characters | |
| `deploy/nginx/*.conf` | `limit_req_zone` | 30/s general · 20/min for sign-in | The only layer that still works when Redis is down |
| `views.py` → `ProfileViewSet` | `SEARCH_MIN_LENGTH` | 2 | |
| `deploy/nginx/*.conf` | `client_max_body_size` | 12 MB | Must stay larger than `MAX_UPLOAD_SIZE` |

---

## 6. Moving this base into an **existing** project that has data

New projects need nothing here. Bringing these changes into a running
project needs one step before migrating:

`0005_email_case_insensitive` adds the unique `Lower(email)` constraint, and
**does not convert the addresses already stored**. Anyone who registered as
`Ali@example.com` will no longer be able to sign in (sign-in now lowercases
what is typed), and the migration itself fails if two addresses differ only
in case.

Check first:

```sql
SELECT LOWER(email), COUNT(*) FROM accounts_user
GROUP BY LOWER(email) HAVING COUNT(*) > 1;
```

No rows? Add a data migration before `0005`:

```python
from django.db.models.functions import Lower


def lowercase_emails(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    # One UPDATE; the database lowercases every address itself.
    User.objects.update(email=Lower("email"))
```

Rows returned? Merge the duplicate accounts by hand first — there is no safe
automatic answer for that.

## 7. Checklist before the first deploy

- [ ] `SECRET_KEY` is new and shared with no other project
- [ ] `DEBUG=False`
- [ ] `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` hold your domain
- [ ] `ADMIN_URL` is not `admin/`
- [ ] `MEDIA_ROOT` is outside the code folder
- [ ] `CELERY_ALWAYS_EAGER=False`
- [ ] `RESERVED_USERNAMES` updated with your project's routes
- [ ] SPF/DKIM/DMARC records set on the mail domain
- [ ] `python manage.py check --deploy` reports no errors
- [ ] `python manage.py test` is green
- [ ] `AXES_ENABLED=True`
- [ ] `ADMIN_URL` updated **in the nginx file too**
- [ ] Behind a CDN: `real_ip_header` set in nginx (**and `NUM_PROXIES` stays `1`**)
- [ ] `limit_req` enabled in nginx (the protection that survives Redis going down)
- [ ] Backups go to an `rclone crypt` destination
- [ ] `SENTRY_DSN` is set
- [ ] You have **restored** a backup at least once
