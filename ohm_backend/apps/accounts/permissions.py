from django.db import connection
from django_tenants.utils import get_public_schema_name
from rest_framework.permissions import BasePermission


class IsTenantAdmin(BasePermission):
    """Require membership in the built-in Tenant Admin group."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.user.groups.filter(name="TENANT_ADMIN").exists()


class DynamicModelPermissions(BasePermission):
    """Apply the Django model permission matching the HTTP method and view model.

    Permissions are resolved at request time, so permissions created by Django
    migrations and assigned through Groups take effect without code changes.
    Safe methods require ``view_<model>``; writes require their matching
    ``add/change/delete_<model>`` permission.
    """

    perms_map = {
        "GET": ["%(app_label)s.view_%(model_name)s"],
        "OPTIONS": [],
        "HEAD": ["%(app_label)s.view_%(model_name)s"],
        "POST": ["%(app_label)s.add_%(model_name)s"],
        "PUT": ["%(app_label)s.change_%(model_name)s"],
        "PATCH": ["%(app_label)s.change_%(model_name)s"],
        "DELETE": ["%(app_label)s.delete_%(model_name)s"],
    }

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True

        queryset = getattr(view, "queryset", None)
        if queryset is None:
            get_queryset = getattr(view, "get_queryset", None)
            if get_queryset is None:
                return False
            queryset = get_queryset()
        model = queryset.model
        required = self.perms_map.get(request.method, [])
        return all(user.has_perm(perm % {
            "app_label": model._meta.app_label,
            "model_name": model._meta.model_name,
        }) for perm in required)


class IsTenantSchema(BasePermission):
    """Allow an endpoint only when the request is routed to a provider schema."""

    message = "This endpoint is only available inside a tenant workspace."

    def has_permission(self, request, view):
        return connection.schema_name != get_public_schema_name()
