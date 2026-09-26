# What to add when you need it — and not before

This base is built for one server, which is the right answer for most
projects. This file collects what a project needs as it grows, in order of
priority.

The other guides: [BUILD-APPS.md](BUILD-APPS.md) · [DEPLOY.md](DEPLOY.md)

**Do not apply anything here pre-emptively.** Every item has a price —
complexity, cost or maintenance — and it is paid in every project while only
a few benefit. Look at your own project first, then take what applies.

Each item is written as: **when** · **why** · **what to do**.

---

## First: security to add before your first real user

### 1. Error reporting (Sentry)

**When:** the first deploy to a real server.
**Why:** without it you do not learn about a 500 until a user complains.
**What:** the library is installed and the code is ready in
`config/settings.py`. Create a project on sentry.io and put the DSN in `.env`
on the server:

```ini
SENTRY_DSN=https://xxxx@oyyy.ingest.sentry.io/zzz
SENTRY_ENVIRONMENT=production
```

This is the only item here that is fully built — it needs a value, nothing
more.

---

### 2. Account lockout after failed attempts — ✅ installed, needs turning on

**When:** at deploy. Now.
**Why:** an IP-based limit (`auth: 10/min`) does not protect one particular
account — a hundred addresses (and they are cheap) means a thousand guesses a
minute at the same account. `django-axes` counts per **account**, so the
attack stops at five attempts however many addresses it comes from.

**What:** nothing to install. The library is there, the settings are written
and the tests exist. One line in `.env` on the server:

```ini
AXES_ENABLED=True
```

It locks an account for an hour after 5 attempts. Leave it `False` locally so
you do not lock yourself out.

**To unlock an account:**

```bash
python manage.py axes_reset_username user@example.com
```

> `AXES_LOCKOUT_PARAMETERS = ['username']` — per account, not per address.
> Axes warns about that (`axes.W006`) because locking by account alone allows
> spraying many accounts from one address — and that axis is already covered
> by `auth: 10/min`. The warning is silenced explicitly in `settings.py`,
> with the reason written next to it.

---

### 3. The CSP header — ✅ installed in report-only mode, needs enforcing

**When:** you display content written by users (comments, product
descriptions, rich text).
**Why:** a second layer underneath HTML sanitising: if a script does get
through, the browser refuses to run it. It matters here in particular because
the JWT is within reach of JavaScript, so one XSS is one stolen account.

**What:** the header is **already present** in
`deploy/nginx/security-headers.conf` as
`Content-Security-Policy-Report-Only` — which blocks nothing and only logs
what *would* have been blocked, so turning it on cannot break a working front
end. Watch the browser console for a few days, widen the policy until the
reports stop, then drop `-Report-Only` from the header name to enforce it:

```nginx
# before:  add_header Content-Security-Policy-Report-Only "..." always;
# after:   add_header Content-Security-Policy             "..." always;
```

A project with no front end can enforce `default-src 'none'` immediately.

And if the front end renders HTML written by users, add sanitising on the
server too:

```bash
pip install nh3     # listed, commented out, in requirements.txt
```

---

### 4. Rate limiting in Nginx — ✅ installed

**Already applied in `deploy/nginx/myproject.conf`:** two zones — `api` at
30/second and `login` at 20/minute covering sign-in, sign-up and password
reset. Nothing to do.

It is not only a performance measure: DRF's limits live in Redis, so a Redis
outage takes them with it (`IGNORE_EXCEPTIONS` keeps the site alive, but
unprotected). This layer is what stands in that gap, and it rejects a flood
before it costs a gunicorn worker at all.

<details><summary>What used to be written here</summary>

**When:** you get a flood of requests, or you see the CPU high for no reason.
**Why:** the current limit is inside Python — the request occupies a whole
gunicorn worker before it is refused. Nginx refuses it before it reaches
Django at all.

**What:** in `nginx.conf`, inside the `http` block:

```nginx
limit_req_zone $binary_remote_addr zone=api:10m rate=30r/s;
```

and in the site file, inside the `location ~ ^/(api|admin)/` block:

```nginx
limit_req zone=api burst=60 nodelay;
```

Set the rate to at least twice normal front-end usage.

</details>

---

### 5. An audit log

**When:** a financial project, or sensitive data, or several staff editing
the same records.
**Why:** to answer "who changed this, and when". It cannot be reconstructed
after the fact.

**What:** `django-simple-history`, on the sensitive models only:

```python
from simple_history.models import HistoricalRecords

class Order(models.Model):
    ...
    history = HistoricalRecords()
```

Do not put it on every model — it doubles the size of the database.

---

## Second: performance

### 6. A functional index for the people search

**When:** the users table passes tens of thousands of rows, or the search
gets slow.
**Why:** a prefix search generates `UPPER(username) LIKE UPPER('no%')` (we
checked). A plain index on `username` **does not serve it** — it needs an
index on the same function.

**What:** in `apps/accounts/models.py`, inside `class Meta` on `User`:

```python
from django.db.models.functions import Upper

class Meta:
    # Keep the ordering as it is — "-id" is a deliberate tiebreaker, and
    # dropping it brings back rows appearing on two pages.
    ordering = ["-created_at", "-id"]
    indexes = [
        models.Index(Upper("username"), name="user_username_upper_idx"),
        models.Index(Upper("first_name"), name="user_first_upper_idx"),
        models.Index(Upper("last_name"), name="user_last_upper_idx"),
    ]
```

Then `makemigrations` and `migrate`.

Before you do, confirm this really is the slow part:

```sql
EXPLAIN ANALYZE SELECT * FROM accounts_user WHERE UPPER(username) LIKE UPPER('no%');
```

`Seq Scan` means yes; `Index Scan` means the problem is elsewhere.

---

### 7. Database connection pooling (PgBouncer)

**When:** you run many gunicorn processes, or you see
`FATAL: sorry, too many clients already`.
**Why:** `CONN_MAX_AGE=60` is connection *reuse*, **not** pooling. Every
gunicorn process holds a connection of its own, and PostgreSQL accepts 100 by
default.

**What:** install PgBouncer in `transaction pooling` mode and point `DB_PORT`
at it (usually 6432) instead of 5432. **And set `CONN_MAX_AGE=0`** — with
PgBouncer, holding the connection becomes harmful rather than helpful.

---

### 8. Caching heavy queries

**When:** after `/silk/` or Sentry has shown you what is actually slow.
**Why:** Redis is configured and running, but nothing is cached — on
purpose. Caching early hides the real problem (a bad query) instead of
solving it.

**What:** fix the query first (`select_related` / `prefetch_related` / an
index). If it is still heavy and rarely changes:

```python
from django.core.cache import cache

def get_categories():
    result = cache.get("categories")
    if result is None:
        result = list(Category.objects.all())
        cache.set("categories", result, 60 * 60)
    return result
```

And remember to invalidate the key on write, or you will serve stale data.

---

## Third: past a single server

These three are **architectural assumptions** in the base, not gaps. They are
correct for one server, and need changing when you add a second.

### 9. Files on object storage

**When:** a second web server, or the disk fills up.
**Why:** `MEDIA_ROOT` is a local path — the second server will not see the
first one's uploads.

**What:**

```bash
pip install django-storages[s3]
```

```python
STORAGES = {
    "default": {"BACKEND": "storages.backends.s3.S3Storage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
AWS_STORAGE_BUCKET_NAME = os.getenv("S3_BUCKET")
AWS_S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT")   # Backblaze B2 or Hetzner are cheaper than AWS
```

Do not forget to move the existing files, and to change `location /media/` in
nginx.

---

### 10. Splitting the scheduler from the worker

**When:** a second worker.
**Why:** `celery -A config worker -B` runs the scheduler **inside** the
process. With a second worker every periodic task runs **twice**.

**What:** remove `-B` from `deploy/systemd/myproject-worker.service` and
create one separate unit, and only one:

```ini
ExecStart=/srv/myproject/app/backend/.venv/bin/celery -A config beat \
    --loglevel=info --schedule=/srv/myproject/celerybeat-schedule
```

---

### 11. Sessions and a load balancer

**When:** more than one web server.
**Why:** `SESSION_ENGINE = cached_db` — reads come from Redis and the row
lives in the database, so a session is shared between servers and survives a
Redis restart ✓. But `/api/health/` checks this server only — the load
balancer should use it to take a broken server out of rotation.

**What:** point the balancer's health check at `GET /api/health/` and treat
`503` as a reason to remove the server.

---

## Fourth: code quality

### 12. Tests for your own apps

**When:** from the first app you write.
**Why:** the 124 tests cover the accounts app only — and that is all a base
can cover. A green CI does not mean your shop's logic is sound.

**What:** read `apps/accounts/tests.py` as a model: every class pins a
**decision**, not a line. Test who has permission, what happens on bad input,
and what must not appear in a public response.

---

### 13. Dependency vulnerability scanning — ✅ installed in CI

**When:** automatically on every push. And by hand before every deploy.
**Why:** **no code reviewer finds this class of problem.** Your code is
correct and the library underneath it is not. We found three known
vulnerabilities this way, one of which disabled
`DATA_UPLOAD_MAX_MEMORY_SIZE` entirely.

**What:** `pip-audit` in `.github/workflows/ci.yml`. By hand — with the same
ignore flags CI uses, or it exits with a failure over five vulnerabilities in
`social-auth-core` (a dependency declared by djoser; none of it runs here,
`social_django` is not in `INSTALLED_APPS`):

```bash
pip install pip-audit
pip-audit -r requirements.txt \
    --ignore-vuln CVE-2026-57175 --ignore-vuln CVE-2026-57176 \
    --ignore-vuln CVE-2026-57177 --ignore-vuln CVE-2026-57178 \
    --ignore-vuln CVE-2026-57179
```

Revisit that list on every djoser upgrade. A new vulnerability? Raise the
version in `requirements.txt`, run the suite, then deploy.

---

### 14. Linting — ✅ installed in CI

**When:** ready. Run it by hand before every push.
**What:** `ruff` — fast, and it replaces both flake8 and isort. Its settings
are in `backend/pyproject.toml` (a deliberately narrow rule set:
`E, W, F, I, UP, B`), and the step is already in
`.github/workflows/ci.yml`:

```bash
pip install ruff
ruff check .
```
