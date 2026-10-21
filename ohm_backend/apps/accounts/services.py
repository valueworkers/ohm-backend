from .models import Role, RoleAssignment, User

MANAGED_ROLES = ("TENANT_ADMIN", "CUSTOMER")


def set_role(user, code, assigned_by=None):
    """Give the user exactly one of the managed tenant-wide roles."""
    RoleAssignment.objects.filter(user=user, scope_type__isnull=True, role__code__in=MANAGED_ROLES).delete()
    RoleAssignment.objects.create(user=user, role=Role.objects.get(code=code), assigned_by=assigned_by)


def ensure_tenant_admin(public_user):
    """Mirror an OHM user into the current tenant schema as its tenant-wide admin."""
    role, _ = Role.objects.get_or_create(
        code=Role.Code.TENANT_ADMIN,
        defaults={"name": "Tenant Admin", "rank": 100},
    )
    user, created = User.objects.get_or_create(
        email=public_user.email.lower(),
        defaults={
            "full_name": public_user.full_name,
            "phone": public_user.phone,
            "is_active": True,
        },
    )
    if created:
        user.password = public_user.password
        user.save(update_fields=["password"])
    elif not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])
    RoleAssignment.objects.get_or_create(
        user=user,
        role=role,
        scope_type=None,
        scope_id=None,
    )
    return user
