#!/usr/bin/env bash
# Update the live site to the latest main. On the server, as the admin user:
#   sudo bash /srv/myproject/app/deploy/deploy.sh
#
# Everything sits in one function called on the last line, because bash reads
# a script as it runs it and `git pull` replaces this very file midway. A
# function is parsed whole first, so the copy in memory is the one that
# finishes; at top level a run would continue from the old file's bytes.
set -euo pipefail

APP=/srv/myproject/app
SERVICE=myproject
RUN_AS="sudo -u $SERVICE -H"

deploy() {
    # Remembered before the pull, so a failed health check can go back to
    # the commit that was serving a minute ago.
    previous=$($RUN_AS git -C "$APP" rev-parse HEAD)

    echo "→ pulling"
    $RUN_AS git -C "$APP" pull --ff-only

    echo "→ backend: dependencies, migrations, default media, static files"
    $RUN_AS "$APP/backend/.venv/bin/pip" install -q -r "$APP/backend/requirements.txt"
    $RUN_AS bash -c "cd '$APP/backend' \
        && .venv/bin/python manage.py migrate --noinput \
        && .venv/bin/python manage.py seed_media_defaults \
        && .venv/bin/python manage.py collectstatic --noinput | tail -1"

    # Delete these two lines for an API-only project.
    echo "→ frontend: build"
    $RUN_AS bash -c "cd '$APP/frontend' && npm ci --no-audit --no-fund && npm run build"

    echo "→ restarting services"
    systemctl restart "$SERVICE-web" "$SERVICE-worker"
    systemctl is-active "$SERVICE-web" "$SERVICE-worker"

    # Proof the new process serves, not just that systemd started it. Both
    # headers are needed: without `Host` gunicorn sees 127.0.0.1, which is not
    # in ALLOWED_HOSTS, and answers 400; without `X-Forwarded-Proto`
    # SECURE_SSL_REDIRECT answers 301, which curl counts as success.
    echo "→ health"
    sleep 2
    host=$(grep -E '^ALLOWED_HOSTS=' "$APP/backend/.env" | tail -n 1 | cut -d= -f2- | cut -d, -f1)
    curl -fsS -H "Host: ${host:-localhost}" -H 'X-Forwarded-Proto: https' \
        http://127.0.0.1:8001/api/health/ || {
        echo
        echo "✗ the new build is not healthy — rolling back to $previous"
        # Exiting here would leave the broken code deployed and running.
        $RUN_AS git -C "$APP" reset --hard "$previous"
        $RUN_AS "$APP/backend/.venv/bin/pip" install -q -r "$APP/backend/requirements.txt"
        systemctl restart "$SERVICE-web" "$SERVICE-worker"
        echo "↩ rolled back. Check: journalctl -u $SERVICE-web -n 50"
        # Migrations are NOT reversed: a rollback cannot guess whether the new
        # ones are safe to undo. If a migration failed, restore the database
        # from deploy/backup.sh too.
        exit 1
    }
    echo

    # As the repository's owner: root running git in a folder it does not own
    # is refused as "dubious ownership".
    echo "✓ deployed $($RUN_AS git -C "$APP" rev-parse --short HEAD)"
}

deploy "$@"
