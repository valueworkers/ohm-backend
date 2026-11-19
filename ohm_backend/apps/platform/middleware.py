"""
Single-domain tenant middleware.

All requests arrive on one API host (e.g. api.o-hm.com).
The active schema is selected by:

  * API requests: ``X-Tenant`` header (absent → public, present → tenant)
  * Admin panel: ``X-Tenant`` form input, cookie, or query param
"""
from django.db import connection
from django.http import JsonResponse
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.utils import get_public_schema_name, get_tenant_model

from .utils import SLUG_RE, schema_for_slug


class TenantHeaderMiddleware(TenantMainMiddleware):
    """Resolve tenants from X-Tenant header, admin login form, or admin session cookie."""

    def _get_public_tenant(self):
        try:
            return get_tenant_model().objects.get(
                schema_name=get_public_schema_name()
            )
        except get_tenant_model().DoesNotExist:
            return None

    def process_request(self, request):
        tenant_slug = request.headers.get("X-Tenant", "").strip().lower()
        is_admin = request.path.startswith("/admin/")

        # For browser admin panel requests without an X-Tenant HTTP header:
        if not tenant_slug and is_admin:
            is_login_post = (
                request.method == "POST"
                and request.path.rstrip("/").endswith("/login")
            )
            if is_login_post:
                # User explicitly submitted the login form
                post_slug = request.POST.get("x_tenant")
                if post_slug is not None:
                    tenant_slug = post_slug.strip().lower()
            else:
                # Normal admin navigation: check query param first, then cookie
                query_slug = request.GET.get("x-tenant") or request.GET.get("x_tenant")
                if query_slug:
                    tenant_slug = query_slug.strip().lower()
                else:
                    cookie_slug = request.COOKIES.get("x_tenant")
                    if cookie_slug:
                        tenant_slug = cookie_slug.strip().lower()

        # ---- No header / slug → public schema (root / platform admin) ----
        if not tenant_slug:
            connection.set_schema_to_public()
            request.tenant = self._get_public_tenant()
            return None

        # ---- X-Tenant / slug present → resolve tenant schema ----
        if not SLUG_RE.fullmatch(tenant_slug):
            if is_admin:
                request._admin_tenant_error = f"Invalid workspace slug '{tenant_slug}'."
                connection.set_schema_to_public()
                request.tenant = self._get_public_tenant()
                return None
            return JsonResponse({"error": "Invalid tenant slug."}, status=400)

        tenant_model = get_tenant_model()
        schema = schema_for_slug(tenant_slug)

        # A schema name supplied through X-Tenant must never select the
        # platform control plane.
        if schema == get_public_schema_name():
            if is_admin:
                request._admin_tenant_error = f"Workspace '{tenant_slug}' not found."
                connection.set_schema_to_public()
                request.tenant = self._get_public_tenant()
                return None
            return JsonResponse({"error": "Tenant not found."}, status=404)

        try:
            tenant = tenant_model.objects.get(schema_name=schema)
        except tenant_model.DoesNotExist:
            if is_admin:
                request._admin_tenant_error = f"Workspace '{tenant_slug}' not found."
                connection.set_schema_to_public()
                request.tenant = self._get_public_tenant()
                return None
            return JsonResponse({"error": "Tenant not found."}, status=404)

        if tenant.schema_name == get_public_schema_name():
            if is_admin:
                request._admin_tenant_error = f"Workspace '{tenant_slug}' not found."
                connection.set_schema_to_public()
                request.tenant = self._get_public_tenant()
                return None
            return JsonResponse({"error": "Tenant not found."}, status=404)

        if tenant.status != "active":
            if is_admin:
                request._admin_tenant_error = f"Workspace '{tenant_slug}' is not active."
                connection.set_schema_to_public()
                request.tenant = self._get_public_tenant()
                return None
            return JsonResponse(
                {"error": "This workspace is not active."},
                status=403,
            )

        connection.set_tenant(tenant)
        request.tenant = tenant
        return None

    def process_response(self, request, response):
        if hasattr(request, "path") and request.path.startswith("/admin/"):
            if request.path.rstrip("/").endswith("/logout"):
                response.delete_cookie("x_tenant", path="/")
            elif getattr(request, "user", None) and request.user.is_authenticated:
                tenant = getattr(request, "tenant", None)
                if tenant and tenant.schema_name != get_public_schema_name():
                    slug = getattr(tenant, "slug", "")
                    if not slug:
                        slug = tenant.schema_name.removeprefix("tenant_")
                    response.set_cookie(
                        "x_tenant",
                        slug,
                        httponly=True,
                        samesite="Lax",
                        path="/",
                    )
                else:
                    response.delete_cookie("x_tenant", path="/")
        return response
