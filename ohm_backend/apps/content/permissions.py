from rest_framework.permissions import BasePermission, SAFE_METHODS


def is_admin_or_manager(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user.groups.filter(name__in=["Tenant Admin", "Super User"]).exists()


class IsSuperUserOrOwnerOrReadOnly(BasePermission):
    """
    Admins have full access.
    All other users: read-only.
    """
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return is_admin_or_manager(request.user)
