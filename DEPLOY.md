# Deploying to a server

Two parts: **the first setup** (once in the life of the project) and **every
deploy after that** (one command).

The example is a project called **shop** on the domain **myshop.com**.
Substitute both names.

---

## Part zero: before you start

You need:

- an Ubuntu server (2 GB of RAM is enough to begin)
- a domain pointing at the server's address (an `A` record)
- an account with a mail service (Resend or Postmark) with SPF/DKIM records
  on your domain
- the project pushed to GitHub

Rename the deployment files **before** you push:

```bash
cd deploy
grep -rl 'myproject\|example\.com' . | xargs sed -i 's/myproject/shop/g; s/example\.com/myshop.com/g'
mv nginx/myproject.conf nginx/shop.conf
for f in systemd/myproject-*; do mv "$f" "${f/myproject/shop}"; done
grep -rn 'myproject\|example\.com' . || echo "clean"
```

---

## Part one: the first setup (once)

### 1. The packages

```bash
sudo apt update && sudo apt install -y \
    python3-venv python3-dev build-essential \
    postgresql postgresql-contrib \
    redis-server nginx certbot python3-certbot-nginx git curl
```

### 2. A user for the service

Never run the application as root: a hole in the code becomes control of the
whole machine.

```bash
sudo adduser --system --group --home /srv/shop shop
sudo mkdir -p /srv/shop/media /srv/shop/backups
sudo chown -R shop:shop /srv/shop
```

> **A security note:** in this guide the service user (`shop`) owns the
> application code, because it is the one running `git pull`. The trade-off:
> a code-execution hole in the application can modify the application's own
> code. For full separation, have a separate `deployer` user own the code and
> give `shop` read-only access to it, while `/srv/shop/media` and
> `/srv/shop/backups` stay owned by `shop`.

### 3. The database

```bash
sudo -u postgres psql
```

```sql
CREATE DATABASE shop;
CREATE USER shop WITH PASSWORD 'put-a-strong-one-here';
ALTER ROLE shop SET client_encoding TO 'utf8';
ALTER ROLE shop SET default_transaction_isolation TO 'read committed';
ALTER ROLE shop SET timezone TO 'UTC';
GRANT ALL PRIVILEGES ON DATABASE shop TO shop;
\c shop
GRANT ALL ON SCHEMA public TO shop;
\q
```

> The last line is required on PostgreSQL 15 and above — without it the first
> migration fails with `permission denied for schema public`.

### 4. The code

```bash
sudo -u shop git clone https://github.com/<you>/shop.git /srv/shop/app
cd /srv/shop/app/backend
sudo -u shop python3 -m venv .venv
sudo -u shop .venv/bin/pip install -r requirements.txt
```

### 5. The `.env` file

```bash
sudo -u shop cp /srv/shop/app/deploy/env.production.example /srv/shop/app/backend/.env
sudo -u shop nano /srv/shop/app/backend/.env
```

Generate the key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

Fill in at least: `SECRET_KEY` · `ALLOWED_HOSTS` · `CSRF_TRUSTED_ORIGINS` ·
`FRONTEND_URL` · `DB_PASSWORD` · `ADMIN_URL` · `MEDIA_ROOT` · `EMAIL_PASS` ·
`DEFAULT_FROM_EMAIL` · **`AXES_ENABLED=True`**. See
[VARIABLES.md](VARIABLES.md).

> `AXES_ENABLED=True` locks an account for an hour after 5 failed sign-ins.
> To unlock one: `python manage.py axes_reset_username user@example.com`

Then protect the file — every secret the project has is in it:

```bash
sudo chmod 600 /srv/shop/app/backend/.env
```

### 6. Migrations and files

```bash
cd /srv/shop/app/backend
sudo -u shop .venv/bin/python manage.py migrate
sudo -u shop .venv/bin/python manage.py backfill_profiles
sudo -u shop .venv/bin/python manage.py seed_media_defaults
sudo -u shop .venv/bin/python manage.py collectstatic --noinput
sudo -u shop .venv/bin/python manage.py createsuperuser
```

### 7. The services

```bash
sudo cp /srv/shop/app/deploy/systemd/shop-*.service /etc/systemd/system/
sudo cp /srv/shop/app/deploy/systemd/shop-*.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shop-web shop-worker
sudo systemctl status shop-web --no-pager
```

Check that the application answers before you add nginx:

```bash
curl -H "Host: myshop.com" -H "X-Forwarded-Proto: https" \
     http://127.0.0.1:8001/api/health/
```

You should see `{"status":"ok","database":"ok","cache":"ok"}`.

> **Both headers are required.** Without `Host` it answers 400 (the address
> is not in `ALLOWED_HOSTS`), and without `X-Forwarded-Proto` it answers 301
> (the HTTPS redirect). They are exactly what nginx sends.

### 8. The certificate, then nginx

**In that order.** The nginx file points at the certificate, and `nginx -t`
fails if it is not there.

```bash
sudo certbot certonly --nginx -d myshop.com -d www.myshop.com
```

Then:

```bash
sudo cp /srv/shop/app/deploy/nginx/security-headers.conf \
        /etc/nginx/snippets/shop-security-headers.conf
sudo cp /srv/shop/app/deploy/nginx/shop.conf /etc/nginx/sites-available/shop
sudo ln -s /etc/nginx/sites-available/shop /etc/nginx/sites-enabled/shop
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

For a project with no front end, delete `root`, `index` and the last two
`location` blocks from the nginx file.

### 9. The firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

The PostgreSQL and Redis ports stay closed — the application reaches them
over `localhost`.

### 10. Backups

**Set `REMOTE=` in `deploy/backup.sh` on your machine and push it before this
step.** The file is tracked in the repository and `deploy.sh` runs
`git pull --ff-only`, so editing it directly on the server makes the first
deploy that touches this file fail with `local changes would be overwritten`.

```bash
sudo apt install -y rclone
sudo -u shop rclone config          # add a crypt destination off the server
sudo systemctl enable --now shop-backup.timer
sudo systemctl start shop-backup.service
sudo journalctl -u shop-backup -n 5 --no-pager
```

> **A backup nobody has restored is not a backup.** Try it once, now:
> ```bash
> sudo -u postgres createdb testrestore
> sudo -u shop pg_restore -d testrestore --no-owner /srv/shop/backups/db-*.dump
> sudo -u postgres dropdb testrestore
> ```

### 11. A final check

```bash
curl -I https://myshop.com/api/health/        # 200
curl -I http://myshop.com                     # 301 to https
curl -I https://www.myshop.com                # 301 to the canonical name
```

Then open `https://myshop.com/<ADMIN_URL>` and sign in.

---

## Part two: every deploy after that

From your machine:

```bash
git push origin main
```

On the server:

```bash
sudo bash /srv/shop/app/deploy/deploy.sh
```

The script does everything, in order:

```
→ pulling                     git pull
→ backend                     pip install · migrate · seed_media_defaults · collectstatic
→ frontend: build             (delete the line if you have no front end)
→ restarting services         systemctl restart shop-web shop-worker
→ health                      confirms the new process actually serves
✓ deployed a1b2c3d
```

**Adding a new app needs nothing extra** — `deploy.sh` runs `migrate`.

The only things that still need a manual step:

| What you did | What you need on the server |
|---|---|
| Added a variable to `.env` | edit `.env` by hand, then `deploy.sh` |
| Changed a systemd file | `cp`, then `daemon-reload`, then `deploy.sh` |
| Changed the nginx file | `cp`, then `nginx -t`, then `reload nginx` |
| Added a periodic task | `deploy.sh` is enough (the worker restarts) |

---

## When something goes wrong

```bash
# the web log, live
sudo journalctl -u shop-web -f

# the last 50 lines, errors only
sudo journalctl -u shop-web -n 50 --no-pager -p err

# the worker
sudo journalctl -u shop-worker -n 50 --no-pager

# are the services running?
sudo systemctl status shop-web shop-worker --no-pager
```

| Symptom | Most likely cause |
|---|---|
| 502 Bad Gateway | `shop-web` is down — check `journalctl -u shop-web` |
| A redirect loop | nginx is not sending `X-Forwarded-Proto` |
| 400 on every request | the domain is not in `ALLOWED_HOSTS` |
| The admin has no styling | `collectstatic` was not run, or `location /static/` is wrong |
| Images 404 | `MEDIA_ROOT` ≠ the `alias` in nginx |
| No mail arrives | check the worker log · check `EMAIL_PASS` and SPF/DKIM |
| CSRF error signing into the admin | the domain is missing from `CSRF_TRUSTED_ORIGINS` (**with** `https://`) |
| The service will not start | usually a missing `.env` key — the message in `journalctl` names it |

**Rolling back quickly:**

```bash
cd /srv/shop/app
sudo -u shop git log --oneline -5
sudo -u shop git reset --hard <the-good-commit>
sudo systemctl restart shop-web shop-worker
```

(If the deploy ran a migration, the rollback needs a database restore too.)

---

## Before every deploy

```bash
python manage.py test            # 124 tests
pip-audit -r requirements.txt \
    --ignore-vuln CVE-2026-57175 --ignore-vuln CVE-2026-57176 \
    --ignore-vuln CVE-2026-57177 --ignore-vuln CVE-2026-57178 \
    --ignore-vuln CVE-2026-57179
```

The second is the only one that finds a hole in a library rather than in your
code — a class of problem no human review catches.

> **Why those five `--ignore-vuln` flags?** `social-auth-core` arrives as a
> dependency of djoser and carries five known CVEs with no fixed release
> compatible with djoser's pin. None of the affected code runs here:
> `social_django` is not in `INSTALLED_APPS` and nothing imports it — djoser
> only declares the dependency. The same list is in
> `.github/workflows/ci.yml`; **revisit it whenever djoser is upgraded.**
> Without these flags the command exits with a failure and you will think
> your project is vulnerable.

## After the first deploy

Turn on the first item in [SCALING.md](SCALING.md) — Sentry. Without it you
will not know about a 500 until a user complains.
