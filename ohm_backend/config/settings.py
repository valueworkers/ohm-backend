import os
from pathlib import Path
from dotenv import load_dotenv
from corsheaders.defaults import default_headers

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

def env(name, default=None):
    return os.environ.get(name, default)


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env("DJANGO_DEBUG", True)

# Host validity is enforced by VerifiedDomainTenantMiddleware:
# only hosts with a *verified* Domain row resolve to a tenant, everything else 404s.
ALLOWED_HOSTS = ["*"]

# --- ohm platform settings -------------------------------------------------
BASE_DOMAIN   = env("OHM_BASE_DOMAIN", "localhost")
PLATFORM_HOST = env("OHM_PLATFORM_HOST", BASE_DOMAIN)
API_HOST = env("OHM_API_HOST", f"api.{BASE_DOMAIN}")
API_HOSTS = {host.strip().lower() for host in env("OHM_API_HOSTS", API_HOST).split(",") if host.strip()}
EDGE_HOST     = env("OHM_EDGE_HOST", f"edge.{BASE_DOMAIN}")
INTERNAL_TOKEN = env("OHM_INTERNAL_TOKEN", "dev-internal-token")  
DOMAIN_VERIFY_PREFIX = "_ohm-verify"                              # TXT record: _ohm-verify.<domain>

ROOT_HOSTS = {
    h.strip().lower()
    for h in env("OHM_ROOT_HOSTS", "127.0.0.1,localhost,ohm.com").split(",")
    if h.strip()
}

OHM_URL_SCHEME = env("OHM_URL_SCHEME", "http")   # "https" in production
OHM_URL_PORT = env("OHM_URL_PORT", ":8000")      # "" in production (used for <slug>.<BASE_DOMAIN> links)
RESERVED_SLUGS = {
    "admin", "www", "api", "app", "edge", "mail", "static", "media", "root", "platform",
    "ohm", "support", "help", "docs", "status", "internal", "public", "login", "register",
}
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in env(
        "OHM_CORS_ALLOWED_ORIGINS",
        "https://ohm.com,http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if origin.strip()
]
CORS_ALLOWED_ORIGIN_REGEXES = [
    regex.strip()
    for regex in env(
        "OHM_CORS_ALLOWED_ORIGIN_REGEXES",
        r"^https://([a-z0-9-]+\.)?ohm\.com$,^http://([a-z0-9-]+\.)?ohm\.localhost:3000$",
    ).split(",")
    if regex.strip()
]

# --- django-tenants ----------------------------------------------------------
# Apps listed in BOTH lists get a table in the public schema (platform staff,
# e.g. Super Admin / Our Team) and one in every tenant schema (tenant users).
SHARED_APPS = [
    "django_tenants",
    "apps.platform",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",
    "rest_framework",
    "corsheaders",
    "apps.accounts",
]

TENANT_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.admin",
    "apps.accounts",
    # Product modules go here later, e.g. "apps.patients", "apps.booking", "apps.billing"
]

INSTALLED_APPS = list(SHARED_APPS) + [a for a in TENANT_APPS if a not in SHARED_APPS]

TENANT_MODEL = "tenants.Tenant"
TENANT_DOMAIN_MODEL = "tenants.Domain"
PUBLIC_SCHEMA_NAME = "public"
PUBLIC_SCHEMA_URLCONF = "config.urls_public"
ROOT_URLCONF = "config.urls"
SHOW_PUBLIC_IF_NO_TENANT_FOUND = False

DATABASES = {
    "default": {
        "ENGINE": "django_tenants.postgresql_backend",
        "NAME": env("POSTGRES_DB", "ohm"),
        "USER": env("POSTGRES_USER", "ohm"),
        "PASSWORD": env("POSTGRES_PASSWORD", "ohm"),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5432"),
    }
}
DATABASE_ROUTERS = ("django_tenants.routers.TenantSyncRouter",)

MIDDLEWARE = [
    "apps.platform.middleware.VerifiedDomainTenantMiddleware",  # must be first
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

AUTH_USER_MODEL = "accounts.User"
WSGI_APPLICATION = "config.wsgi.application"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.auth.TenantJWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_THROTTLE_RATES": {
        "root_login": "20/minute",
        "tenant_registration_create": "20/hour",
        "onboarding_request": "10/hour",
    },
    "NUM_PROXIES": 1,  # Caddy in front
}

CORS_ALLOW_HEADERS = [*default_headers, "x-tenant"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --- Celery ------------------------------------------------------------------
CELERY_BROKER_URL = env("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("REDIS_URL", "redis://localhost:6379/0")
CELERY_BEAT_SCHEDULE = {
    "verify-pending-domains": {
        "task": "apps.platform.tasks.verify_pending_domains",
        "schedule": 300,
    },
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
