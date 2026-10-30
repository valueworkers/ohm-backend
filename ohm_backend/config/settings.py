from datetime import timedelta
import os
from pathlib import Path
from dotenv import load_dotenv
from corsheaders.defaults import default_headers
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

def env(name, default=None):
    return os.environ.get(name, default)

def env_bool(name, default=False):
    value = env(name)
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean value.")

def env_csv(name, default):
    value = env(name)
    if value is None:
        return list(default)
    return [item.strip() for item in str(value).split(",") if item.strip()]

REDIS_URL = env("REDIS_URL", "redis://localhost:6379/0")


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
if not DEBUG and SECRET_KEY == "dev-insecure-change-me":
    raise ImproperlyConfigured("Set a unique DJANGO_SECRET_KEY when DEBUG is disabled.")

# Host validity is enforced by VerifiedDomainTenantMiddleware:
# only hosts with a *verified* Domain row resolve to a tenant, everything else 404s.
ALLOWED_HOSTS = env_csv(
    "DJANGO_ALLOWED_HOSTS",
    ["*"] if DEBUG else [env("OHM_API_HOST", "api.o-hm.com"), env("OHM_BASE_DOMAIN", "o-hm.com"), "." + env("OHM_BASE_DOMAIN", "o-hm.com")],
)

# --- ohm platform settings -------------------------------------------------
BASE_DOMAIN   = env("OHM_BASE_DOMAIN", "localhost")
API_HOST      = env("OHM_API_HOST", f"api.{BASE_DOMAIN}")
INTERNAL_TOKEN = env("OHM_INTERNAL_TOKEN", "dev-internal-token")

OHM_URL_SCHEME = env("OHM_URL_SCHEME", "http")   # "https" in production

RESERVED_SLUGS = {
    'app','demo','internal','ops','root','ftp',
    'edge','mail','dashboard','status','staging',
    'login','register','o-hm','help','public','ohm',
    'api','smtp','www','docs','support','media',
    'platform','admin','test','static'
}


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
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "apps.platform.middleware.TenantHeaderMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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
# ASGI_APPLICATION = "config.asgi.application"

REST_FRAMEWORK = {
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],

    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.auth.TenantJWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "apps.accounts.permissions.DynamicModelPermissions",
    ],
    "DEFAULT_PAGINATION_CLASS": "config.pagination.StandardResultsSetPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_THROTTLE_RATES": {
        "root_login": "20/minute",
        "login_identity": "10/hour",
        "refresh_token": "30/hour",
        "otp_request": "5/hour",
        "otp_identity": "5/hour",
        "otp_verify": "10/hour",
        "sensitive_auth": "5/hour",
        "tenant_registration_create": "20/hour",
        "tenant_request": "10/hour",
        "settings_email": "5/hour",
    },
    "NUM_PROXIES": 1,
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': False,  # revocation is enforced by schema-local UserSession rows
    'UPDATE_LAST_LOGIN': True,
}

CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", DEBUG)
CORS_ALLOWED_ORIGINS = env_csv("CORS_ALLOWED_ORIGINS", [])
CORS_ALLOW_HEADERS = [*default_headers, "x-tenant"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", 31536000))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", True)



# --- Storage & static ------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# --- Email ------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("EMAIL_PORT", 587))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "noreply@ohm.local")
QUOTE_AMP_ALLOWED_SENDERS = env_csv("QUOTE_AMP_ALLOWED_SENDERS", [DEFAULT_FROM_EMAIL])

OHM_SMS_OTP_BACKEND = env("OHM_SMS_OTP_BACKEND", "")

# --- Cache ------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "ohm",
    }
}
# --- Celery ------------------------------------------------------------------
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_BEAT_SCHEDULE = {
    "verify-pending-domains": {
        "task": "apps.platform.tasks.verify_pending_domains",
        "schedule": 300,
    },
    "expire-sent-quotes-daily": {
        "task": "apps.platform.tasks.expire_sent_quotes",
        "schedule": 86400,
    },
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
