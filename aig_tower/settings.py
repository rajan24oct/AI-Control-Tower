"""Django settings for the AI Control Tower."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
CSRF_TRUSTED_ORIGINS = env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
)

DJANGO_APPS = [
    # Must precede staticfiles so `runserver` serves ASGI and WebSockets work
    # in development without a separate process.
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
]

THIRD_PARTY_APPS = [
    "channels",
    "rest_framework",
    "django_filters",
]

# Order matters: dependencies first so string model references resolve cleanly.
LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.organizations",
    "apps.audit",
    "apps.providers",
    "apps.models_registry",
    "apps.agents",
    "apps.applications_registry",
    "apps.executions",
    "apps.telemetry",
    "apps.costs",
    "apps.risks",
    "apps.approvals",
    "apps.policies",
    "apps.routing",
    "apps.gateway",
    "apps.evaluations",
    "apps.notifications",
    "apps.inventory",
    "apps.dashboard",
    "apps.api",
    "apps.integrations",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.audit.middleware.AuditContextMiddleware",
    "apps.organizations.middleware.ActiveOrganizationMiddleware",
]

ROOT_URLCONF = "aig_tower.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.organizations.context_processors.organization_context",
            ],
        },
    },
]

WSGI_APPLICATION = "aig_tower.wsgi.application"
ASGI_APPLICATION = "aig_tower.asgi.application"

if env_bool("USE_SQLITE", True):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            # SQLite is the local-dev fallback only. WAL lets readers continue
            # during a long write (seeding, bulk telemetry ingest) instead of
            # failing with "database is locked"; PostgreSQL needs none of this.
            "OPTIONS": {
                "timeout": 20,
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "aig_tower"),
            "USER": os.environ.get("POSTGRES_USER", "aig_tower"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "aig_tower"),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }

AUTH_USER_MODEL = "accounts.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard:home"
LOGOUT_REDIRECT_URL = "accounts:login"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
# Manifest storage requires `collectstatic` to have run, so it is opt-in via
# env (docker-compose sets it) rather than breaking local runs and tests.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if env_bool("USE_MANIFEST_STATIC", False)
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

# Console backend: notification emails are printed, never sent anywhere.
# Point this at a real SMTP backend deliberately, not by accident.
EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
DEFAULT_FROM_EMAIL = os.environ.get("DJANGO_FROM_EMAIL", "ai-control-tower@localhost")
SITE_URL = os.environ.get("SITE_URL", "")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.api.authentication.ServiceAccountAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "apps.api.permissions.IsAuthenticatedOrganizationMember",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.api.pagination.ControlTowerPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/1")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE

# Live execution monitor (section 25). Redis carries broadcasts between web
# workers in production; the in-memory layer keeps local runs and tests
# single-process and dependency-free.
if env_bool("USE_SQLITE", True):
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [os.environ.get("CHANNEL_LAYER_URL", REDIS_URL)]},
        }
    }

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache"
        if env_bool("USE_SQLITE", True)
        else "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    X_FRAME_OPTIONS = "DENY"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "handlers": ["console"], "propagate": False},
    },
}

# Control Tower domain defaults.
CONTROL_TOWER = {
    "DEFAULT_CURRENCY": "USD",
    # Risk score (likelihood x impact, 1-25) -> level. Configurable per section 17.
    "RISK_THRESHOLDS": {"LOW": 4, "MODERATE": 9, "HIGH": 16},
    "AGENT_AUTONOMY_APPROVAL_THRESHOLD": 3,
    # Prompt and completion text on the observability surface. Nothing is
    # captured implicitly -- this only governs how far what engines *do* report
    # is shown. Classified agents are redacted even for holders of prompt.view,
    # so a shared operations screen never becomes an accidental data export.
    # AI Gateway. These are the numbers that decide how the platform behaves
    # when it is in the request path and something upstream is broken, so they
    # are configuration rather than constants buried in the pipeline.
    "GATEWAY": {
        # Ceiling on one provider call.
        "ATTEMPT_TIMEOUT_SECONDS": float(os.environ.get("GATEWAY_ATTEMPT_TIMEOUT", "30")),
        # Ceiling on the whole request, fallbacks included. A chain that would
        # start an attempt past this point stops instead: better a clear 502
        # than a caller waiting on a fourth try nobody budgeted for.
        "REQUEST_DEADLINE_SECONDS": float(os.environ.get("GATEWAY_DEADLINE", "60")),
        # Consecutive failures before a provider is taken out of the chain.
        "CIRCUIT_FAILURE_THRESHOLD": int(os.environ.get("GATEWAY_CIRCUIT_THRESHOLD", "5")),
        # How long an open breaker waits before letting one probe through.
        "CIRCUIT_COOL_OFF_SECONDS": int(os.environ.get("GATEWAY_CIRCUIT_COOL_OFF", "30")),
        # Hard-stop budgets refuse traffic. Set false to make every budget
        # advisory again without editing each one.
        "ENFORCE_BUDGETS": env_bool("GATEWAY_ENFORCE_BUDGETS", True),
    },
    "PROMPT_DISPLAY": {
        "MAX_CHARS": 1200,
        "REDACT_CLASSIFICATIONS": env_list(
            "PROMPT_REDACT_CLASSIFICATIONS", "confidential,restricted"
        ),
    },
}
