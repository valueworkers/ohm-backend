from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.utils import get_public_schema_name, get_tenant_model

from .utils import SLUG_RE, schema_for_slug

TENANT_HEADER = "X-Tenant"

# Caddy uses this endpoint before normal tenant host resolution.
INTERNAL_PATHS = ("/internal/domain-check/",)


def _deny(detail, status):
    return JsonResponse({"detail": detail}, status=status)


class VerifiedDomainTenantMiddleware(TenantMainMiddleware):
    """Single API host: X-Tenant selects a tenant schema; no header means the public schema."""

    def process_request(self, request):
        if request.path in INTERNAL_PATHS:
            return self._use_public(request)

        if self.hostname_from_request(request).lower() not in settings.API_HOSTS:
            return _deny("Not found.", 404)

        tenant_slug = request.headers.get(TENANT_HEADER, "").strip().lower()
        if not tenant_slug:
            return self._use_public(request)
        return self._use_tenant(request, tenant_slug)

    def _use_public(self, request):
        connection.set_schema_to_public()
        request.tenant = connection.tenant
        request.urlconf = settings.PUBLIC_SCHEMA_URLCONF
        return None

    def _use_tenant(self, request, tenant_slug):
        connection.set_schema_to_public()
        if not SLUG_RE.fullmatch(tenant_slug):
            return _deny("Invalid tenant.", 404)

        tenant_model = get_tenant_model()
        tenant = (
            tenant_model.objects
            .filter(schema_name=schema_for_slug(tenant_slug), domains__verified=True)
            .exclude(schema_name=get_public_schema_name())
            .distinct()
            .first()
        )
        if tenant is None:
            return _deny("Tenant not found.", 404)
        if tenant.status == tenant_model.Status.SUSPENDED:
            return _deny("This workspace is suspended.", 403)
        if tenant.status != tenant_model.Status.ACTIVE:
            return _deny("Tenant not found.", 404)

        connection.set_tenant(tenant)
        request.tenant = tenant
        request.urlconf = settings.ROOT_URLCONF
        return None