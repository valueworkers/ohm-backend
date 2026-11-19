from django.db import connection
from django_tenants.utils import get_public_schema_name
from rest_framework.permissions import BasePermission

def ModuleEnabled(code):
    """DRF permission: the current tenant must have this product module enabled."""

    class _ModuleEnabled(BasePermission):
        message = f"Module '{code}' is not enabled for this workspace."

        def has_permission(self, request, view):
            from apps.accounts.permissions import is_root_user, is_superadmin
            if request.user and (is_superadmin(request.user) or is_root_user(request.user)):
                return True
            tenant = getattr(connection, "tenant", None)
            return code in (getattr(tenant, "modules", None) or [])

    return _ModuleEnabled

class IsPlatformAdmin(BasePermission):
    """O-HM super admins & root staff: Django superusers, PlatformAdmin profiles, or staff."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        from apps.accounts.permissions import is_root_user, is_superadmin
        return bool(
            user.is_superuser
            or hasattr(user, "platform_admin_profile")
            or user.is_staff
            or is_superadmin(user)
            or is_root_user(user)
        )

