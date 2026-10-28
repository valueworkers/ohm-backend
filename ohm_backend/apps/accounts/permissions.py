from django.db import connection
from django_tenants.utils import get_public_schema_name
from rest_framework.permissions import BasePermission, DjangoModelPermissions
from apps.platform.permissions import IsPlatformAdmin
from .models import Role


class IsTenantAdmin(BasePermission):
    """Require membership in the built-in Tenant Admin group."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if connection.schema_name == get_public_schema_name():
            return False
        return (
            request.user.groups.filter(name="TENANT_ADMIN").exists()
            or request.user.role_assignments.filter(
                role__code=Role.Code.TENANT_ADMIN,
                scope_type__isnull=True,
                scope_id__isnull=True,
            ).exists()
        )


class DynamicModelPermissions(DjangoModelPermissions):
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

class IsTenantSchema(BasePermission):
    """Allow an endpoint only when the request is routed to a provider schema."""

    message = "This endpoint is only available inside a tenant workspace."

    def has_permission(self, request, view):
        return connection.schema_name != get_public_schema_name()


class IsSchemaAdministrator(BasePermission):
    """Platform administrators in public; tenant administrators in a tenant schema."""

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if connection.schema_name == get_public_schema_name():
            return IsPlatformAdmin().has_permission(request, view)
        return IsTenantAdmin().has_permission(request, view)
