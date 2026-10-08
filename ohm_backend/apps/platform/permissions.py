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


class IsRootUser(BasePermission):
    """Allow only active platform superusers to manage public-schema tenants."""

    message = "Only an authenticated root user can perform this action."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            connection.schema_name == get_public_schema_name()
            and user
            and user.is_authenticated
            and user.is_active
            and user.is_superuser
        )


class RootUserOrOnboardingApplicant(BasePermission):
    """Allow root admins or applicants presenting their private request token."""

    def has_permission(self, request, view):
        return IsRootUser().has_permission(request, view) or bool(
            request.headers.get("X-Onboarding-Token")
        )
