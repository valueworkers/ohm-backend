from django.db import connection
from django_tenants.utils import get_public_schema_name
from rest_framework.permissions import BasePermission, DjangoModelPermissions
from apps.platform.permissions import IsPlatformAdmin
def is_superadmin(user):
    """Check if the user is a superadmin."""
    if not (user and user.is_authenticated):
        return False
    return bool(
        user.is_superuser
        or user.groups.filter(name__in=["Super User", "SUPER_ADMIN", "Superadmin", "SUPERADMIN", "Root Admin", "ROOT_ADMIN"]).exists()
    )


def is_root_user(user):
    """Check if the user is a platform root user / staff."""
    if not (user and user.is_authenticated):
        return False
    return bool(
        user.is_staff
        or hasattr(user, "platform_admin_profile")
        or user.groups.filter(name__in=[
            "OPS_MANAGER", "STAFF", "Ops Manager", "Staff Member",
            "Root Admin", "ROOT_ADMIN", "Platform Admin", "PLATFORM_ADMIN",
        ]).exists()
    )


def is_root_admin(user):
    """Check if the user is any platform root administrator (superadmin, staff, platform admin profile)."""
    return is_superadmin(user) or is_root_user(user)


def is_tenant_admin(user):
    """Check if the user has Tenant Admin privileges in the current tenant schema."""
    if not (user and user.is_authenticated):
        return False
    if is_superadmin(user) or is_root_user(user):
        return True
    if connection.schema_name == get_public_schema_name():
        return False
    return bool(
        user.groups.filter(name__in=["TENANT_ADMIN", "Tenant Admin"]).exists()
    )


class IsTenantAdmin(BasePermission):
    """Require membership in the built-in Tenant Admin group or Root Admin privileges."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if is_superadmin(request.user) or is_root_user(request.user):
            return True
        return is_tenant_admin(request.user)


class CanManageRoles(BasePermission):
    """Allow Superadmin and Root users across all schemas, and Tenant Admin in tenant schemas."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if is_superadmin(user) or is_root_user(user):
            return True
        if is_tenant_admin(user):
            return True
        return False

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

    def has_permission(self, request, view):
        if getattr(view, "_ignore_model_permissions", False):
            return True

        if not request.user or (
            not request.user.is_authenticated and self.authenticated_users_only
        ):
            return False

        if is_superadmin(request.user) or is_root_user(request.user) or is_tenant_admin(request.user):
            return True

        try:
            queryset = self._queryset(view)
        except (AssertionError, AttributeError):
            return True

        if queryset is None or getattr(queryset, "model", None) is None:
            return True

        perms = self.get_required_permissions(request.method, queryset.model)

        for perm in perms:
            if not self._check_perm(request.user, perm):
                return False
        return True

    def _check_perm(self, user, perm):
        if user.has_perm(perm):
            return True
        if "." in perm:
            app_label, codename = perm.split(".", 1)
            return user.groups.filter(
                permissions__content_type__app_label=app_label,
                permissions__codename=codename,
            ).exists()
        return False


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
        if is_superadmin(request.user) or is_root_user(request.user):
            return True
        if connection.schema_name == get_public_schema_name():
            return IsPlatformAdmin().has_permission(request, view)
        return is_tenant_admin(request.user)
