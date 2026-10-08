from .models import Role, RoleAssignment

MANAGED_ROLES = ("TENANT_ADMIN", "CUSTOMER")


def set_role(user, code, assigned_by=None):
    """Give the user exactly one of the managed tenant-wide roles."""
    RoleAssignment.objects.filter(user=user, scope_type__isnull=True, role__code__in=MANAGED_ROLES).delete()
    RoleAssignment.objects.create(user=user, role=Role.objects.get(code=code), assigned_by=assigned_by)
