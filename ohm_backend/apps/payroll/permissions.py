from rest_framework.permissions import BasePermission, SAFE_METHODS
from apps.accounts.permissions import is_root_user, is_superadmin


def is_admin_or_manager(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff or is_superadmin(user) or is_root_user(user):
        return True
    return user.groups.filter(name__in=["Tenant Admin", "Super User", "Branch Manager"]).exists()


class RootAdminThrottleExemptMixin:
    """Keep platform root administration available during tenant traffic spikes."""

    def get_throttles(self):
        user = getattr(self.request, "user", None)
        if is_superadmin(user) or is_root_user(user):
            return []
        return super().get_throttles()


class IsOwnerOrReadOnly(BasePermission):
    """Only admins, root users, and superusers may change attendance configuration."""

    def has_permission(self, request, view):
        if request.user and (is_superadmin(request.user) or is_root_user(request.user)):
            return True
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return is_admin_or_manager(request.user)


class CanViewSalaryReport(BasePermission):
    """
    Object-level permission for salary reports.
    - Superuser/Root Admin/Manager: can view anyone's report(s).
    - Everyone else: can only view their own report(s).
    """

    message = "You do not have permission to view this report."

    def has_permission(self, request, view):
        if request.user and (is_superadmin(request.user) or is_root_user(request.user)):
            return True
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        target_user = getattr(obj, "user", obj)
        request_user = request.user

        if is_superadmin(request_user) or is_root_user(request_user) or is_admin_or_manager(request_user):
            return True

        return target_user.id == request_user.id


class IsPayrollAdminOrSelfReadOnly(BasePermission):
    """Limit payroll mutations to administrators while retaining self-service reads."""

    message = "Only payroll administrators can modify payroll records."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.method in SAFE_METHODS or is_admin_or_manager(request.user)

    def has_object_permission(self, request, view, obj):
        if is_admin_or_manager(request.user):
            return True
        if request.method not in SAFE_METHODS:
            return False
        return getattr(obj, "user_id", request.user.id) == request.user.id
