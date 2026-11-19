from django.contrib.auth import get_user_model
from apps.accounts.permissions import is_root_user, is_superadmin

from apps.payroll.models import Attendance

User = get_user_model()


def is_admin_or_manager(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff or is_superadmin(user) or is_root_user(user):
        return True
    return user.groups.filter(name__in=["Tenant Admin", "Super User", "Branch Manager"]).exists()


class PermissionScopeMixin:
    """
    Provides get_attendance_queryset() and get_user_queryset() scoped
    to the requesting user's role.
    """

    def get_user_queryset(self, request):
        user = request.user
        manager = getattr(User.objects, "employees", User.objects.all)
        base_qs = manager()

        if is_admin_or_manager(user):
            return base_qs
        return base_qs.filter(pk=user.pk)

    def get_attendance_queryset(self, request):
        user = request.user
        if is_admin_or_manager(user):
            return Attendance.objects.all()
        return Attendance.objects.filter(user=user)
