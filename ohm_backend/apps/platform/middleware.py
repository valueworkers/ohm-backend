from django.conf import settings
from django.db import connection
from django.http import HttpResponse
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.utils import get_public_schema_name, get_tenant_model

from .utils import SLUG_RE, schema_for_slug

# Caddy uses this endpoint before normal tenant host resolution.
INTERNAL_PATHS = ("/internal/domain-check/",)


class VerifiedDomainTenantMiddleware(TenantMainMiddleware):
    """Resolve tenants from verified hostnames or the API's X-Tenant workspace header."""

    def get_tenant(self, domain_model, hostname):
        domain = domain_model.objects.select_related("tenant").get(domain__iexact=hostname, verified=True)
        return domain.tenant

    def process_request(self, request):
        if request.path in INTERNAL_PATHS:
            connection.set_schema_to_public()
            request.urlconf = settings.PUBLIC_SCHEMA_URLCONF
            return None

        hostname = self.hostname_from_request(request).lower()
        tenant_slug = request.headers.get("X-Tenant", "").strip().lower()
        if hostname in settings.API_HOSTS and tenant_slug:
            connection.set_schema_to_public()
            if not SLUG_RE.fullmatch(tenant_slug):
                return HttpResponse("Invalid tenant.", status=404)
            tenant_model = get_tenant_model()
            try:
                tenant = tenant_model.objects.get(schema_name=schema_for_slug(tenant_slug))
            except tenant_model.DoesNotExist:
                return HttpResponse("Tenant not found.", status=404)
            if tenant.schema_name == get_public_schema_name() or not tenant.domains.filter(verified=True).exists():
                return HttpResponse("Tenant not found.", status=404)
            if tenant.status == "suspended":
                return HttpResponse("This workspace is suspended.", status=403)
            if tenant.status != "active":
                return HttpResponse("Tenant not found.", status=404)
            connection.set_tenant(tenant)
            request.tenant = tenant
            request.urlconf = settings.ROOT_URLCONF
            return None

        response = super().process_request(request)
        if response is not None:
            return response

        tenant = getattr(request, "tenant", None)
        if tenant is not None and getattr(tenant, "status", None) == "suspended":
            return HttpResponse("This workspace is suspended.", status=403)
        return None
