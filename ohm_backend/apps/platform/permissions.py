from django.db import connection
from django_tenants.utils import get_public_schema_name
from rest_framework.permissions import BasePermission


def ModuleEnabled(code):
    """DRF permission: the current tenant must have this product module enabled."""

    class _ModuleEnabled(BasePermission):
        message = f"Module '{code}' is not enabled for this workspace."

        def has_permission(self, request, view):
            tenant = getattr(connection, "tenant", None)
            return code in (getattr(tenant, "modules", None) or [])

    return _ModuleEnabled

class IsPlatformAdmin(BasePermission):
    """O-HM super admins: Django superusers or users with a PlatformAdmin profile."""

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (user.is_superuser or hasattr(user, "platform_admin_profile"))
        )
