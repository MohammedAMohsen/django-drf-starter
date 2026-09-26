"""Project settings.

One file, not a package: every deployment difference lives in .env, and the
real behaviour changes are the `if not DEBUG` and `if TESTING` blocks below.
"""

import os
import sys
from datetime import timedelta
from pathlib import Path

from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def required_env(name):
    """A setting the process must not start without.

    A missing key returns None, which travels quietly: the server boots and
    the first query fails with an error that names nothing.
    """
    value = os.getenv(name)
    if not value:
        raise ImproperlyConfigured(f"{name} is not set. Add it to .env")
    return value


def env_list(name, default=''):
    # ''.split(',') gives [''] — one empty host, not no hosts.
    return [item.strip() for item in os.getenv(name, default).split(',') if item.strip()]


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# sys.argv[1], not `"test" in sys.argv`: the loose form would put
# `migrate --database test` into test mode.
TESTING = (
    (len(sys.argv) > 1 and sys.argv[1] == "test")
    or os.getenv("PYTEST_VERSION") is not None
)

DEBUG = env_bool('DEBUG', False)
SECRET_KEY = required_env('SECRET_KEY')
ALLOWED_HOSTS = env_list('ALLOWED_HOSTS')

# Empty ALLOWED_HOSTS with DEBUG off answers 400 to everything and explains
# nothing. Fail at startup instead.
if not DEBUG and not TESTING and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS is empty while DEBUG is off — set it in .env")

# Development only: the front end is same-origin in production. The default
# applies only under DEBUG, so a server never trusts localhost.
CORS_ALLOWED_ORIGINS = env_list(
    'CORS_ALLOWED_ORIGINS',
    'http://localhost:5173,http://127.0.0.1:5173' if DEBUG else '',
)

# Needs the scheme, unlike ALLOWED_HOSTS.
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS')

# Host only, no scheme. Every mailed link is built from it.
DOMAIN = os.getenv('FRONTEND_URL', 'localhost:5173')
SITE_NAME = os.getenv('SITE_NAME', 'MyProject')
EMAIL_FRONTEND_PROTOCOL = "http" if DEBUG else "https"

# The front-end pages our emails link to, defined once: djoser reads two of
# them below and emails.py builds all three from here.
FRONTEND_PATHS = {
    'activate': 'activate/{uid}/{token}',
    'reset_password': 'reset-password/{uid}/{token}',
    'confirm_email': 'confirm-email/{uid}/{token}',
}

# `or 'admin'`, not a getenv default: a default applies only to a *missing*
# key, and `ADMIN_URL=` left empty became '/', so the admin vanished.
ADMIN_URL = (os.getenv('ADMIN_URL') or 'admin').strip('/') + '/'

# Empty is fine — /api/v1/google/ then refuses every token.
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sitemaps',

    # Third-party
    'djoser',
    'corsheaders',
    'django_redis',
    'django_filters',
    'rest_framework',
    'drf_spectacular',
    'django_extensions',
    'rest_framework_simplejwt',
    'django_cleanup.apps.CleanupConfig',
    'rest_framework_simplejwt.token_blacklist',

    # Local apps
    'apps.accounts',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'config.middleware.NoIndexApiMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# silk records every request — headers, body, SQL — behind no authentication.
# `not TESTING` as well, because its own queries break any query-count test.
if DEBUG and not TESTING:
    INSTALLED_APPS.append('silk')
    MIDDLEWARE.append('silk.middleware.SilkyMiddleware')

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
AUTH_USER_MODEL = 'accounts.User'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Returns the user even when is_active is False, so sign-in can tell "wrong
# password" from "correct password, not activated" without checking twice.
# Nothing loosens: SimpleJWT still refuses an inactive user's tokens.
AUTHENTICATION_BACKENDS = ['django.contrib.auth.backends.AllowAllUsersModelBackend']

# Account lockout (django-axes), installed but off by default. The throttles
# below count per IP address, and addresses are cheap: a hundred of them turn
# `auth: 10/min` into a thousand guesses a minute at one account. Axes counts
# per account instead.
AXES_ENABLED = env_bool('AXES_ENABLED', False)

if AXES_ENABLED:
    INSTALLED_APPS.append('axes')
    MIDDLEWARE.append('axes.middleware.AxesMiddleware')
    # Axes first, so a locked account is refused before any password check;
    # AllowAllUsersModelBackend must stay behind it (LoginSerializer).
    AUTHENTICATION_BACKENDS = [
        'axes.backends.AxesStandaloneBackend',
        *AUTHENTICATION_BACKENDS,
    ]
    AXES_FAILURE_LIMIT = 5
    AXES_COOLOFF_TIME = 1  # hours
    AXES_LOCKOUT_PARAMETERS = ['username']
    # axes.W006 warns that locking by username alone allows spraying many
    # accounts from one address — covered by `auth: 10/min` above.
    SILENCED_SYSTEM_CHECKS = ['axes.W006']

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': required_env('DB_NAME'),
        'USER': required_env('DB_USER'),
        # Optional: peer authentication on a local socket takes no password.
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', ''),  # empty = Unix socket
        'PORT': os.getenv('DB_PORT', '5432'),
        # Django's default of 0 reconnects on every request. Zero under test:
        # a held connection can stop the test database being dropped.
        'CONN_MAX_AGE': 0 if TESTING else 60,
        # A reused connection can be closed at the far end; without this the
        # first query on a stale one is the one that fails.
        'CONN_HEALTH_CHECKS': True,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 10},  # Django's default is 8
    },
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True


STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'  # collectstatic target, served by Nginx

MEDIA_URL = '/media/'
# On a server this points outside the repository, so a fresh clone or a reset
# never touches the uploads.
MEDIA_ROOT = Path(os.getenv('MEDIA_ROOT', BASE_DIR / 'media'))

# The built front end, if there is one. See config/spa.py.
SPA_INDEX = Path(os.getenv('SPA_INDEX', BASE_DIR.parent / 'frontend' / 'dist' / 'index.html'))
SERVE_SPA = env_bool('SERVE_SPA', False)

FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024  # above this, stream to disk
FILE_UPLOAD_PERMISSIONS = 0o644
# A larger body is refused before any view runs. 12 MB leaves room over the
# 10 MB image ceiling for multipart overhead.
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000


# EMAIL_HOST keeps a default on purpose: unset, smtplib is handed None and
# every send dies with the misleading "please run connect() first".
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv('EMAIL_USER')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_PASS')
EMAIL_TIMEOUT = 10
# Its own variable: with a relay such as Resend the SMTP user is the word
# "resend", while the sender must be an address on our own domain.
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL') or os.getenv('EMAIL_USER') or 'no-reply@localhost'
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Print mail instead of needing an SMTP account to read an activation link.
if DEBUG and not EMAIL_HOST_USER:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'


REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    # Nothing is public unless a view says so; the opposite default means one
    # forgotten permission_classes silently exposes an endpoint.
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    # The version is a path segment: /api/v1/... An unknown one is a 404,
    # not a silent fallback.
    'DEFAULT_VERSIONING_CLASS': 'rest_framework.versioning.URLPathVersioning',
    'DEFAULT_VERSION': 'v1',
    'ALLOWED_VERSIONS': ['v1'],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 10,
    # One shape for every error the API returns.
    'EXCEPTION_HANDLER': 'config.exceptions.api_exception_handler',
    # Unset, DRF keys the throttle on the whole X-Forwarded-For string,
    # including the part the client sent — so rotating the header bought a
    # fresh counter every request. Stays 1 behind a CDN too, because Nginx
    # *overwrites* the header rather than appending; behind Cloudflare the fix
    # is `real_ip_header CF-Connecting-IP` there, not a higher number here.
    'NUM_PROXIES': 1,
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.ScopedRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '120/min',
        'user': '300/min',
        # Endpoints that trade a secret for a session, or send mail.
        'auth': '10/min',
        # People search: the two-character minimum stops it being a plain
        # directory, and this stops a patient crawl through every pair.
        'search': '20/min',
    },
}

# The browsable API is a development convenience and a wider surface in
# production for no benefit.
if DEBUG:
    REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES'] = [
        'rest_framework.renderers.JSONRenderer',
        'rest_framework.renderers.BrowsableAPIRenderer',
    ]
else:
    REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES'] = ['rest_framework.renderers.JSONRenderer']


SPECTACULAR_SETTINGS = {
    'TITLE': f'{SITE_NAME} API',
    'DESCRIPTION': f'{SITE_NAME} endpoints',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SCHEMA_PATH_PREFIX': '/api/v[0-9]/',
    'COMPONENT_SPLIT_REQUEST': True,
    # spectacular serves its own views with AllowAny and ignores
    # DEFAULT_PERMISSION_CLASSES, so the schema was a public map of every
    # endpoint and field. Staff-only on a server, open on a laptop.
    'SERVE_PERMISSIONS': (
        ['rest_framework.permissions.AllowAny'] if DEBUG
        else ['rest_framework.permissions.IsAdminUser']
    ),
}


SIMPLE_JWT = {
    # Both accepted: `Authorization: JWT <t>` or `Bearer <t>`. (spectacular
    # warns it can document only one; harmless.)
    'AUTH_HEADER_TYPES': ('JWT', 'Bearer'),
    # The window in which a token that should be dead still works: after an
    # email change, a logout elsewhere, a ban. Rotation means an active
    # session never notices.
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    # How long a user may stay away, not how long a session lasts: the
    # refresh token is rotated on every use and the spent one blacklisted,
    # which is what makes a stolen one near worthless.
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'CHECK_USER_IS_ACTIVE': True,
    'UPDATE_LAST_LOGIN': True,
    # Without this, changing the password removes nobody: the intruder's
    # refresh token stays valid for thirty days. The token carries a hash of
    # the password and stops matching the moment it changes.
    'CHECK_REVOKE_TOKEN': True,
    'REVOKE_TOKEN_CLAIM': 'hash_password',
}


DJOSER = {
    # We issue JWTs; djoser's own Token would be a second credential nobody
    # revokes.
    'TOKEN_MODEL': None,
    # /api/v1/auth/users/ must not be a directory of every account.
    'HIDE_USERS': True,
    'PASSWORD_RESET_CONFIRM_RETYPE': True,
    'SET_PASSWORD_RETYPE': True,
    'SEND_ACTIVATION_EMAIL': True,
    'ACTIVATION_URL': FRONTEND_PATHS['activate'],
    'PASSWORD_RESET_CONFIRM_URL': FRONTEND_PATHS['reset_password'],
    # Same answer whether or not the address has an account, so the form
    # cannot be used to find out who is registered here.
    'PASSWORD_RESET_SHOW_EMAIL_NOT_FOUND': False,
    # If it was not the owner who changed it, this mail is how they find out.
    'PASSWORD_CHANGED_EMAIL_CONFIRMATION': True,
    'SERIALIZERS': {
        'user_create': 'apps.accounts.serializers.CustomUserCreateSerializer',
        'user': 'apps.accounts.serializers.CustomUserSerializer',
        'current_user': 'apps.accounts.serializers.CustomUserSerializer',
        # These two look the user up by exact email match, and addresses are
        # stored lowercase — so a capital letter meant no mail and no error.
        'password_reset': 'apps.accounts.serializers.LowercasedEmailSerializer',
        'username_reset': 'apps.accounts.serializers.LowercasedEmailSerializer',
        # Its own token generator, so an activation link cannot be replayed
        # as a password reset.
        'activation': 'apps.accounts.serializers.ActivationSerializer',
    },
    'EMAIL': {
        'activation': 'apps.accounts.djoser_services.CeleryActivationEmail',
        'password_reset': 'apps.accounts.djoser_services.CeleryPasswordResetEmail',
        'password_changed_confirmation': 'apps.accounts.djoser_services.CeleryPasswordChangedEmail',
    },
}


# Safe in every environment. nosniff matters most for /media/: it stops a
# browser rendering an uploaded file as HTML on our own domain.
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_REFERRER_POLICY = 'same-origin'
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # the SPA reads this one to send X-CSRFToken
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'

if not DEBUG and not TESTING:
    # `not TESTING` because the test client speaks plain HTTP, so
    # SSL_REDIRECT would 301 every test before it reached a view.
    #
    # Nginx ends the TLS connection and talks plain HTTP to gunicorn, so this
    # header is the only way Django knows a request arrived over HTTPS.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
    # A one-year commitment with no early way back, and preload ships the
    # rule built into browsers. Off for subdomains you do not control.
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('HSTS_INCLUDE_SUBDOMAINS', True)
    SECURE_HSTS_PRELOAD = env_bool('HSTS_PRELOAD', True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


# One Redis, three databases: 1 cache, 2 Celery queue, 3 Celery results.
REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379')

CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': f'{REDIS_URL}/1',
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            # Every throttle reads the cache, so without this a Redis restart
            # answered 500 everywhere. The trade-off: with the cache gone the
            # throttles keep no counters, which is why `limit_req` in
            # deploy/nginx/ has to exist — otherwise this trades an outage
            # for an open door.
            'IGNORE_EXCEPTIONS': True,
        },
    }
}

# A swallowed exception nobody logs is a fault nobody learns about.
DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = True

# cached_db, not cache: reads come from Redis but the row survives a restart,
# which otherwise signs every administrator out mid-task.
SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'


CELERY_BROKER_URL = f'{REDIS_URL}/2'
CELERY_RESULT_BACKEND = f'{REDIS_URL}/3'
CELERY_TASK_ACKS_LATE = True
# A task is handed out only when a worker is free, so one slow job does not
# sit in a busy worker's queue while another idles.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_SOFT_TIME_LIMIT = 240
# With Redis down, .delay() raises instead of hanging the web request.
CELERY_BROKER_TRANSPORT_OPTIONS = {'socket_timeout': 5}

# Development only: tasks run inline, no broker and no worker. On a server a
# slow task would block the request and a failed one 500 a good signup.
if env_bool('CELERY_ALWAYS_EAGER', False):
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True

# Celery's own scheduler reads this — no extra package, no table. The worker
# runs it with -B, which suits exactly one worker; a second runs every job
# twice.
CELERY_BEAT_SCHEDULE = {
    # Releases addresses held by a change request nobody confirmed.
    'clear-stale-pending-emails': {
        'task': 'apps.accounts.tasks.clear_stale_pending_emails',
        'schedule': crontab(hour=4, minute=0),
    },
    # Rotation writes two rows per renewal and nothing else removes them.
    'flush-expired-tokens': {
        'task': 'apps.accounts.tasks.flush_expired_tokens',
        'schedule': crontab(hour=4, minute=30),
    },
}


# Without this, a 500 behind gunicorn is a bare line in journalctl with no
# traceback.
LOG_LEVEL = os.getenv('LOG_LEVEL', 'DEBUG' if DEBUG else 'INFO')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '{levelname} {asctime} {name} {message}', 'style': '{'},
    },
    'handlers': {
        # stdout, which systemd captures into journalctl.
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
    },
    'root': {'handlers': ['console'], 'level': LOG_LEVEL},
    'loggers': {
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
        # Uncomment while hunting an N+1 — prints every statement.
        # 'django.db.backends': {'handlers': ['console'], 'level': 'DEBUG'},
    },
}


# Without a DSN nothing is imported and nothing is sent.
SENTRY_DSN = os.getenv('SENTRY_DSN', '')

if SENTRY_DSN and not DEBUG and not TESTING:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        # Fraction of requests recorded for timing. 1.0 until traffic is real.
        traces_sample_rate=float(os.getenv('SENTRY_TRACES_SAMPLE_RATE', '0.1')),
        # On, this attaches the user, cookies and request bodies to every
        # report — which is how tokens reach someone else's server.
        send_default_pii=False,
        environment=os.getenv('SENTRY_ENVIRONMENT', 'production'),
    )


if TESTING:
    # Not the development cache: cached values and throttle counters leak
    # through it in both directions.
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
    SESSION_ENGINE = 'django.contrib.sessions.backends.db'

    # Uploads land in a scratch folder, never among real media.
    import atexit
    import shutil
    import tempfile
    MEDIA_ROOT = Path(tempfile.mkdtemp(prefix='test-media-'))
    atexit.register(shutil.rmtree, MEDIA_ROOT, ignore_errors=True)

    # PBKDF2 is deliberately slow, which is waste in a suite that creates a
    # user per test.
    PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True
    EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

    # Quiet by default. TEST_LOG=ERROR shows the traceback behind a 500.
    LOGGING['root']['level'] = os.getenv('TEST_LOG', 'CRITICAL')
