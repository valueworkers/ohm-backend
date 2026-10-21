import logging
from django.contrib.auth.models import Group, Permission as AuthPermission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django_tenants.utils import get_public_schema_name, schema_context

from .models import Permission as AccountsPermission, Role, RoleAssignment, User

logger = logging.getLogger(__name__)

MANAGED_ROLES = ("TENANT_ADMIN", "CUSTOMER")

# --------------------------------------------------------------------------
# Role / Group permission mapping rules
# --------------------------------------------------------------------------
GROUP_RULES = {
    # OHM Platform Super Admin gets every permission in the system
    Role.Code.SUPER_ADMIN: "__all__",

    # Tenant Admin gets full access across tenant modules
    Role.Code.TENANT_ADMIN: {
        "apps": ["accounts", "venue_manager", "booking", "payroll", "notification", "analysis", "wallet", "faq"],
        "actions": "__all__",
    },

    # Ops Manager can view/change/add across operational apps
    Role.Code.OPS_MANAGER: {
        "apps": ["accounts", "venue_manager", "booking", "payroll", "notification", "analysis", "wallet", "faq"],
        "actions": ["view", "change", "add"],
    },

    Role.Code.VENUE_ADMIN: {
        "apps": ["venue_manager", "booking", "payroll", "notification"],
        "actions": ["view", "change", "add"],
    },

    Role.Code.SERVICE_ADMIN: {
        "apps": ["venue_manager", "booking", "payroll", "notification"],
        "actions": ["view", "change", "add"],
    },

    Role.Code.RESOURCE_ADMIN: {
        "apps": ["venue_manager", "booking", "payroll", "notification"],
        "actions": ["view", "change", "add"],
    },

    # Staff can view
    Role.Code.STAFF: {
        "apps": ["venue_manager", "booking", "payroll", "notification"],
        "actions": ["view"],
    },

    # Customer can view & add on booking, wallet, accounts
    Role.Code.CUSTOMER: {
        "apps": ["booking", "wallet", "accounts"],
        "actions": ["view", "add"],
    },
}

# Public root user (non-superuser platform staff) default permissions
PUBLIC_ROOT_RULES = {
    "apps": ["tenants", "platform", "accounts"],
    "actions": ["view", "change", "add"],
}

ROLE_METADATA = {
    Role.Code.SUPER_ADMIN: {"name": "Super Admin", "rank": 100},
    Role.Code.TENANT_ADMIN: {"name": "Tenant Admin", "rank": 100},
    Role.Code.OPS_MANAGER: {"name": "Ops Manager", "rank": 80},
    Role.Code.VENUE_ADMIN: {"name": "Venue Admin", "rank": 70},
    Role.Code.SERVICE_ADMIN: {"name": "Service Admin", "rank": 70},
    Role.Code.RESOURCE_ADMIN: {"name": "Resource Admin", "rank": 70},
    Role.Code.STAFF: {"name": "Staff Member", "rank": 50},
    Role.Code.CUSTOMER: {"name": "Customer / User", "rank": 10},
}


def sync_accounts_permissions():
    """Ensure every auth.Permission in the current schema has a matching accounts.Permission record."""
    auth_perms = AuthPermission.objects.select_related("content_type").all()
    created_list = []
    existing_codes = set(AccountsPermission.objects.values_list("code", flat=True))
    for ap in auth_perms:
        code = f"{ap.content_type.app_label}.{ap.codename}"
        if code not in existing_codes:
            created_list.append(
                AccountsPermission(
                    code=code,
                    name=ap.name,
                    description=f"{ap.name} for {ap.content_type.app_label}.{ap.content_type.model}",
                )
            )
            existing_codes.add(code)
    if created_list:
        AccountsPermission.objects.bulk_create(created_list, ignore_conflicts=True)


def get_auth_permissions_for_rule(rule):
    """Return auth.Permission queryset matching rule specifications."""
    if rule == "__all__":
        return AuthPermission.objects.all()

    apps = rule.get("apps", [])
    actions = rule.get("actions", [])

    cts = ContentType.objects.filter(app_label__in=apps)
    if not cts.exists():
        return AuthPermission.objects.none()

    if actions == "__all__":
        return AuthPermission.objects.filter(content_type__in=cts)

    actions_pattern = "|".join(actions)
    return AuthPermission.objects.filter(
        content_type__in=cts,
        codename__regex=f"^({actions_pattern})_",
    )


def get_accounts_permissions_for_auth_perms(auth_perms):
    """Return matching accounts.Permission queryset for given auth.Permission objects."""
    if not auth_perms.exists():
        return AccountsPermission.objects.none()
    codes = [f"{p.content_type.app_label}.{p.codename}" for p in auth_perms.select_related("content_type")]
    return AccountsPermission.objects.filter(code__in=codes)


def seed_roles_and_groups_for_schema(is_public=False):
    """Seed all default groups, roles, and their assigned permissions in the current schema."""
    sync_accounts_permissions()

    roles_to_seed = (
        [Role.Code.SUPER_ADMIN, Role.Code.OPS_MANAGER, Role.Code.STAFF]
        if is_public
        else list(GROUP_RULES.keys())
    )

    for role_code in roles_to_seed:
        rule = GROUP_RULES.get(role_code)
        if not rule:
            continue

        meta = ROLE_METADATA.get(role_code, {"name": role_code.replace("_", " ").title(), "rank": 50})

        # Configure Django auth.Group
        group, _ = Group.objects.get_or_create(name=role_code)
        auth_perms = get_auth_permissions_for_rule(rule)
        group.permissions.set(auth_perms)

        # Configure accounts.Role
        role, _ = Role.objects.get_or_create(
            code=role_code,
            defaults={"name": meta["name"], "rank": meta["rank"]},
        )
        accounts_perms = get_accounts_permissions_for_auth_perms(auth_perms)
        role.permissions.set(accounts_perms)


def assign_all_permissions_to_superuser(user):
    """Provide ALL permissions to a super user of the public schema."""
    public_schema = get_public_schema_name()
    with schema_context(public_schema):
        seed_roles_and_groups_for_schema(is_public=True)

        all_auth_perms = AuthPermission.objects.all()
        user.user_permissions.set(all_auth_perms)

        super_group = Group.objects.filter(name=Role.Code.SUPER_ADMIN).first()
        if super_group:
            user.groups.add(super_group)

        super_role = Role.objects.filter(code=Role.Code.SUPER_ADMIN).first()
        if super_role:
            RoleAssignment.objects.get_or_create(
                user=user,
                role=super_role,
                scope_type=None,
                scope_id=None,
            )

        updated_fields = []
        if not user.is_superuser:
            user.is_superuser = True
            updated_fields.append("is_superuser")
        if not user.is_staff:
            user.is_staff = True
            updated_fields.append("is_staff")
        if updated_fields:
            user.save(update_fields=updated_fields)

        logger.info("Assigned all permissions to superuser %s in public schema.", user.email)


def assign_default_permissions_to_public_root_user(user):
    """Provide default permissions to a non-superuser public root user (staff/platform admin)."""
    public_schema = get_public_schema_name()
    with schema_context(public_schema):
        seed_roles_and_groups_for_schema(is_public=True)

        default_perms = get_auth_permissions_for_rule(PUBLIC_ROOT_RULES)
        user.user_permissions.set(default_perms)

        staff_group = (
            Group.objects.filter(name=Role.Code.OPS_MANAGER).first()
            or Group.objects.filter(name=Role.Code.STAFF).first()
        )
        if staff_group:
            user.groups.add(staff_group)

        staff_role = (
            Role.objects.filter(code=Role.Code.OPS_MANAGER).first()
            or Role.objects.filter(code=Role.Code.STAFF).first()
        )
        if staff_role:
            RoleAssignment.objects.get_or_create(
                user=user,
                role=staff_role,
                scope_type=None,
                scope_id=None,
            )

        if not user.is_staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])

        logger.info("Assigned default root permissions to %s in public schema.", user.email)


def seed_tenant_default_permissions(schema_name):
    """Seed default permissions, groups, and roles for a tenant schema."""
    with schema_context(schema_name):
        seed_roles_and_groups_for_schema(is_public=False)
        logger.info("Default permissions, roles, and groups seeded for schema %s.", schema_name)


def assign_tenant_admin_permissions(user):
    """Assign full tenant-admin default permissions and group to a tenant admin user."""
    role, _ = Role.objects.get_or_create(
        code=Role.Code.TENANT_ADMIN,
        defaults={"name": "Tenant Admin", "rank": 100},
    )
    RoleAssignment.objects.get_or_create(
        user=user,
        role=role,
        scope_type=None,
        scope_id=None,
    )
    admin_group = Group.objects.filter(name=Role.Code.TENANT_ADMIN).first()
    if admin_group:
        user.groups.add(admin_group)

    tenant_admin_perms = get_auth_permissions_for_rule(GROUP_RULES[Role.Code.TENANT_ADMIN])
    user.user_permissions.set(tenant_admin_perms)
    logger.info("Assigned default tenant admin permissions to %s in current schema.", user.email)


def assign_role_default_permissions(user, role_code):
    """Assign default permissions and group matching a role code to a user."""
    group = Group.objects.filter(name=role_code).first()
    if group:
        user.groups.add(group)

    rule = GROUP_RULES.get(role_code)
    if rule:
        perms = get_auth_permissions_for_rule(rule)
        user.user_permissions.set(perms)


def set_role(user, code, assigned_by=None):
    """Give the user exactly one of the managed tenant-wide roles and matching default permissions."""
    RoleAssignment.objects.filter(user=user, scope_type__isnull=True, role__code__in=MANAGED_ROLES).delete()
    role = Role.objects.get(code=code)
    RoleAssignment.objects.create(user=user, role=role, assigned_by=assigned_by)
    assign_role_default_permissions(user, code)


def ensure_tenant_admin(public_user):
    """Mirror an OHM user into the current tenant schema as its tenant-wide admin with default permissions."""
    seed_roles_and_groups_for_schema(is_public=False)

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
    else:
        updated_fields = []
        if not user.is_active:
            user.is_active = True
            updated_fields.append("is_active")
        if public_user.password and user.password != public_user.password:
            user.password = public_user.password
            updated_fields.append("password")
        if not user.full_name and public_user.full_name:
            user.full_name = public_user.full_name
            updated_fields.append("full_name")
        if not user.phone and public_user.phone:
            user.phone = public_user.phone
            updated_fields.append("phone")
        if updated_fields:
            user.save(update_fields=updated_fields)

    assign_tenant_admin_permissions(user)
    return user


def sync_tenant_roles_and_permissions(tenant):
    """
    For a given Tenant instance:
    1. Seeds default roles, groups, and permissions in its schema.
    2. Mirrors every owner in tenant.owners into the tenant schema as TENANT_ADMIN.
    3. Assigns TENANT_ADMIN role, group, and default permissions to each owner.
    4. Synchronizes groups and permissions for any other existing users in the tenant schema
       who have role assignments.
    Returns dict with summary details: {
        'tenant': tenant.schema_name,
        'assigned_owners': [emails],
        'synced_users': [emails]
    }
    """
    from apps.platform.models import TenantProvisioning

    public_schema = get_public_schema_name()
    with schema_context(public_schema):
        owners = list(tenant.owners.all())
        if not owners:
            provisioning = (
                tenant.provisioning_requests.filter(status="provisioned").first()
                or tenant.provisioning_requests.first()
            )
            if provisioning and provisioning.applicant:
                tenant.owners.add(provisioning.applicant)
                owners.append(provisioning.applicant)
                for email in (provisioning.co_owner_emails or []):
                    email = email.strip().lower()
                    if email and email != provisioning.applicant.email.lower():
                        from apps.accounts.signals import _get_or_create_public_user
                        co_user = _get_or_create_public_user(email)
                        tenant.owners.add(co_user)
                        owners.append(co_user)

    assigned_owners = []
    synced_users = []

    with schema_context(tenant.schema_name):
        seed_roles_and_groups_for_schema(is_public=False)

        for owner in owners:
            admin_user = ensure_tenant_admin(owner)
            assigned_owners.append(admin_user.email)

        for user in User.objects.all():
            assignments = user.role_assignments.select_related("role").all()
            if not assignments.exists():
                continue
            for ra in assignments:
                role_code = ra.role.code
                if role_code == Role.Code.TENANT_ADMIN:
                    assign_tenant_admin_permissions(user)
                else:
                    assign_role_default_permissions(user, role_code)
            synced_users.append(user.email)

    return {
        "tenant": tenant.schema_name,
        "assigned_owners": assigned_owners,
        "synced_users": list(set(synced_users)),
    }


def sync_public_schema_roles_and_permissions():
    """
    Seeds roles/groups in public schema, and assigns all permissions to superusers
    and default permissions to staff/platform admin users.
    Returns dict with summary details: {
        'schema': public_schema,
        'superusers': [emails],
        'staff_users': [emails]
    }
    """
    from apps.platform.models import PlatformAdmin

    public_schema = get_public_schema_name()
    superusers = []
    staff_users = []

    with schema_context(public_schema):
        seed_roles_and_groups_for_schema(is_public=True)

        for superuser in User.objects.filter(is_superuser=True):
            assign_all_permissions_to_superuser(superuser)
            superusers.append(superuser.email)

        for staff_user in User.objects.filter(is_staff=True, is_superuser=False):
            assign_default_permissions_to_public_root_user(staff_user)
            staff_users.append(staff_user.email)

        for pa in PlatformAdmin.objects.select_related("user"):
            if not pa.user.is_superuser and pa.user.email not in staff_users:
                assign_default_permissions_to_public_root_user(pa.user)
                staff_users.append(pa.user.email)

    return {
        "schema": public_schema,
        "superusers": superusers,
        "staff_users": staff_users,
    }


def assign_roles_and_permissions_to_existing_tenants_and_owners(target_schema=None):
    """
    Coordinates seeding and assigning roles/permissions across public and tenant schemas.
    """
    from apps.platform.models import Tenant

    public_schema = get_public_schema_name()
    results = {}

    if not target_schema or target_schema == public_schema:
        results[public_schema] = sync_public_schema_roles_and_permissions()

    if target_schema and target_schema != public_schema:
        tenants = Tenant.objects.filter(schema_name=target_schema)
    else:
        tenants = Tenant.objects.exclude(schema_name=public_schema)

    for tenant in tenants:
        results[tenant.schema_name] = sync_tenant_roles_and_permissions(tenant)

    return results

