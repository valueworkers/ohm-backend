import re
from django.conf import settings

from .models import (
    Domain,
    Tenant,
    TenantProvisioning,
)

# Lowercase, starts with a letter, 3-30 chars, no leading/trailing hyphen.
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,28}[a-z0-9]$")
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

RESERVED_SLUGS = {
    "www", "api", "admin", "root", "public", "static", "media", "mail", "smtp",
    "ftp", "app", "dashboard", "login", "register", "support", "help", "docs",
    "status", "ohm", "o-hm", "ops", "staging", "test", "demo",
}


def base_domain():
    return getattr(settings, "TENANT_BASE_DOMAIN", "o-hm.com")


def slug_problem(slug, exclude_pk=None):
    """Return a human-readable reason the slug can't be used, or None if it's free."""
    if not SLUG_RE.match(slug):
        return "Use 3-30 lowercase letters, numbers or hyphens, starting with a letter."
    if slug in RESERVED_SLUGS:
        return "This subdomain is reserved."
    schema_names = {slug, slug.replace("-", "_")}
    if Tenant.objects.filter(schema_name__in=schema_names).exists():
        return "This subdomain is already taken."
    if Domain.objects.filter(domain=f"{slug}.{base_domain()}").exists():
        return "This subdomain is already taken."
    open_requests = TenantProvisioning.objects.filter(
        slug=slug, status__in=TenantProvisioning.OPEN_STATUSES
    )
    if exclude_pk:
        open_requests = open_requests.exclude(pk=exclude_pk)
    if open_requests.exists():
        return "This subdomain is already requested."
    return None


def schema_for_slug(slug):
    """Return the database schema name used for a workspace slug."""
    return "t_" + slug.strip().lower().replace("-", "_")
