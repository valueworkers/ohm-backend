import re
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from .models import (
    Domain,
    Tenant,
    TenantProvisioning,
)

# Lowercase, starts with a letter, 3-30 chars, no leading/trailing hyphen.
SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,28}[a-z0-9]$")
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

def base_domain():
    return getattr(settings, "TENANT_BASE_DOMAIN", "o-hm.com")


def tenant_url(slug, path=""):
    scheme = getattr(settings, "TENANT_URL_SCHEME", "https")
    return f"{scheme}://{slug}.{base_domain()}{path}"



def send_tenant_welcome_email(p, tenant):
    login_url = "https://admin.o-hm.com/login"
    profile_url = "TBD"  # TODO: link to the profile page in the tenant schema, once that exists)

    ctx = {
        "organization_name": p.organization_name,
        "email": p.email,
        "workspace_host": f"{p.slug}.{base_domain()}",
        "login_url": login_url,
        "profile_url": profile_url,
    }
    subject = f"Welcome to O-HM, {p.organization_name} is approved"
    text = (
        f"Hi,\n\n"
        f"Your request for {p.organization_name} has been approved and your workspace is ready.\n\n"
        f"1. Open {login_url}, enter {p.email} and choose 'Login with OTP'.\n"
        f"2. Complete your profile at {profile_url}, then activate your workspace.\n\n"
        f"If you didn't request this, please ignore this email.\n\n"
        f"The O-HM team"
    )
    html = render_to_string("tenant_welcome.html", ctx)

    msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [p.email])
    msg.attach_alternative(html, "text/html")
    msg.send()


def slug_problem(slug, exclude_pk=None):
    """Return a human-readable reason the slug can't be used, or None if it's free."""
    if not SLUG_RE.match(slug):
        return "Use 3-30 lowercase letters, numbers or hyphens, starting with a letter."
    if slug in settings.RESERVED_SLUGS:
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
    """Return the database schema name used for a workspace slug.

    Must stay in sync with ``services.schema_name_for``.
    """
    return slug.strip().lower().replace("-", "_")
