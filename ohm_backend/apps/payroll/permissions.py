from rest_framework.permissions import BasePermission, SAFE_METHODS


def is_admin_or_manager(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user.groups.filter(name__in=["Tenant Admin", "Super User", "Branch Manager"]).exists()


class IsOwnerOrReadOnly(BasePermission):
    """Only admins and superusers may change attendance configuration."""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return is_admin_or_manager(request.user)


class CanViewSalaryReport(BasePermission):
    """
    Object-level permission for salary reports.
    - Superuser/Admin: can view anyone's report(s).
    - Everyone else: can only view their own report(s).
    """

    message = "You do not have permission to view this report."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        target_user = getattr(obj, "user", obj)
        request_user = request.user

        if is_admin_or_manager(request_user):
            return True

        return target_user.id == request_user.id
