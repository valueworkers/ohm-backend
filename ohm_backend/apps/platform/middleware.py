"""
Single-domain tenant middleware.

All requests arrive on one API host (e.g. api.o-hm.com).
The active schema is selected by the ``X-Tenant`` header:

  * Header absent  → public schema  (platform admin / root user)
  * Header present → tenant schema  (tenant users)
"""
from django.db import connection
from django.http import JsonResponse
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.utils import get_public_schema_name, get_tenant_model

from .utils import SLUG_RE, schema_for_slug


class TenantHeaderMiddleware(TenantMainMiddleware):
    """Resolve tenants exclusively from the ``X-Tenant`` request header."""

    def process_request(self, request):
        tenant_slug = request.headers.get("X-Tenant", "").strip().lower()

        # ---- No header → public schema (root / platform admin) ----
        if not tenant_slug:
            connection.set_schema_to_public()
            try:
                request.tenant = get_tenant_model().objects.get(
                    schema_name=get_public_schema_name()
                )
            except get_tenant_model().DoesNotExist:
                # First run before bootstrap_platform; let Django keep going.
                request.tenant = None
            return None

        # ---- X-Tenant present → resolve tenant schema ----
        if not SLUG_RE.fullmatch(tenant_slug):
            return JsonResponse({"error": "Invalid tenant slug."}, status=400)

        tenant_model = get_tenant_model()
        schema = schema_for_slug(tenant_slug)
        # A schema name supplied through X-Tenant must never select the
        # platform control plane. The public Tenant row exists for routing
        # root requests, but it is not a provider workspace.
        if schema == get_public_schema_name():
            return JsonResponse({"error": "Tenant not found."}, status=404)

        try:
            tenant = tenant_model.objects.get(schema_name=schema)
        except tenant_model.DoesNotExist:
            return JsonResponse({"error": "Tenant not found."}, status=404)

        # Keep this check even though `schema` is normalized above: the
        # resolved Tenant record is the final authority for the selected
        # schema.
        if tenant.schema_name == get_public_schema_name():
            return JsonResponse({"error": "Tenant not found."}, status=404)

        if tenant.status == "suspended":
            return JsonResponse(
                {"error": "This workspace is suspended."},
                status=403,
            )


        connection.set_tenant(tenant)
        request.tenant = tenant
        return None
