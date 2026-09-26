# Starting a new project from this base

From the copy to a server running on your machine. Every command here has
been run.

The example is a project called **Shop**, with a database called `shop_db`.
Substitute both names.

Once this works, go to [BUILD-APPS.md](BUILD-APPS.md) to build your own apps.

---

## Requirements

Three things must be **installed and running**:

| Requirement | Check it |
|---|---|
| Python 3.12+ | `python3 --version` |
| PostgreSQL | `psql -l` |
| Redis | `ss -lntp \| grep 6379` |

**Run Redis in development too.** The site does *not* fall over without it —
`IGNORE_EXCEPTIONS` in the settings makes the cache fail quietly and requests
are still served (verified: pages answer 200 and `/api/health/` answers 503
`degraded`). But without Redis, **rate limiting stops counting**, and
background tasks need a broker unless you set `CELERY_ALWAYS_EAGER=True`.
To start it:

```bash
sudo systemctl start redis-server && sudo systemctl enable redis-server
```

(The tests alone do not need it — `manage.py test` switches the cache to
in-memory.)

---

## 1. Copy the folder and clean it

```bash
cd ~/Projects
cp -r DjangoStarter Shop
cd Shop
rm -rf .git backend/.venv backend/.env backend/media backend/staticfiles backend/.ruff_cache
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
```

> **Why delete those?** `.venv` holds absolute paths to the old project, so
> it will not work, and `.env` holds the old `SECRET_KEY` — keeping it means
> two projects share one key, so breaking into one opens the other.

## 2. The virtual environment

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> A virtual environment is a folder holding a Python installation that
> belongs to this project alone. You will see `(.venv)` at the start of your
> shell prompt after `source`, and you need that line again in every new
> terminal.

## 3. The `.env` file

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

Open `.env` and change only these:

```ini
SECRET_KEY=paste-the-generated-key-here
SITE_NAME=Shop
DB_NAME=shop_db
DB_USER=your-postgres-user
DB_PASSWORD=              # empty with local peer authentication
DB_HOST=                  # empty = connect over a local socket
CELERY_ALWAYS_EAGER=True  # tasks run inline, no worker needed
```

Leave the rest. The details are in [VARIABLES.md](VARIABLES.md).

## 4. The database and the migrations

```bash
createdb shop_db
python manage.py migrate
python manage.py seed_media_defaults
```

> `seed_media_defaults` copies the default avatar and cover into place.
> Without it every new account shows a broken image.

## 5. Check that the copy is sound

```bash
python manage.py test
```

You should see `Ran 124 tests ... OK`. **If anything fails here, stop and fix
it before you write a line of your own code** — the problem is in the copy,
not in your project.

## 6. Run it

```bash
python manage.py createsuperuser
python manage.py runserver
```

| Address | What you get |
|---|---|
| `http://127.0.0.1:8000/api/schema/swagger-ui/` | every endpoint, ready to try |
| `http://127.0.0.1:8000/admin/` | the admin |
| `http://127.0.0.1:8000/api/health/` | `{"status": "ok"}` |
| `http://127.0.0.1:8000/silk/` | the query profiler (development only) |

Try it from Swagger: `POST /api/v1/auth/users/` to register — the activation
link appears **in your terminal**, because mail is printed instead of sent in
development.

## 7. Fit the base to your project

| File | What to change |
|---|---|
| `apps/accounts/validators/user.py` | `RESERVED_USERNAMES` — add your own top-level routes |
| `apps/accounts/models.py` | `Profile` fields: drop `github`/`linkedin`, add what you need |
| `apps/accounts/emails.py` | the wording of the five messages |
| `templates/emails/` | the look of the messages (the colour `#111827`) |

After any change to `models.py`:

```bash
python manage.py makemigrations && python manage.py migrate
```

## 8. Start git

```bash
cd ..
git init && git add . && git commit -m "Initial commit — from DjangoStarter"
```

Make sure `.env` was not committed:

```bash
git ls-files | grep -x 'backend/.env' && echo "DANGER" || echo "safe"
```

---

## Every command at once

```bash
cd ~/Projects
cp -r DjangoStarter Shop && cd Shop
rm -rf .git backend/.venv backend/.env backend/media backend/staticfiles backend/.ruff_cache
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null

cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
# ← edit .env now
createdb shop_db
python manage.py migrate
python manage.py seed_media_defaults
python manage.py test
python manage.py createsuperuser
python manage.py runserver
```

**Next:** [BUILD-APPS.md](BUILD-APPS.md) — building your apps on top of the base.
