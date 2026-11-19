from rest_framework.permissions import BasePermission, SAFE_METHODS
from apps.accounts.permissions import is_root_user, is_superadmin


def is_admin_or_manager(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff or is_superadmin(user) or is_root_user(user):
        return True
    return user.groups.filter(name__in=["Tenant Admin", "Super User"]).exists()


class IsSuperUserOrOwnerOrReadOnly(BasePermission):
    """
    Admins, root users, and managers have full access.
    All other users: read-only.
    """
    def has_permission(self, request, view):
        if request.user and (is_superadmin(request.user) or is_root_user(request.user)):
            return True
        if request.method in SAFE_METHODS:
            return True
        return is_admin_or_manager(request.user)
