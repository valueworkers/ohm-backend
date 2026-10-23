import logging
from django.contrib.auth.models import Group
from django.db import connection
from django_tenants.utils import get_public_schema_name, schema_context

from .access import TenantAccessManager
from .models import Role, RoleAssignment, User

logger = logging.getLogger(__name__)

MANAGED_ROLES = ("TENANT_ADMIN", "CUSTOMER")

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


def seed_roles_and_groups_for_schema(is_public=False):
    """Ensure the built-in role groups exist without overwriting their permissions."""

    roles_to_seed = (
        [Role.Code.SUPER_ADMIN, Role.Code.OPS_MANAGER, Role.Code.STAFF]
        if is_public
        else list(Role.Code.values)
    )

    for role_code in roles_to_seed:
        meta = ROLE_METADATA.get(role_code, {"name": role_code.replace("_", " ").title(), "rank": 50})
        Group.objects.get_or_create(name=role_code)

        # Configure accounts.Role
        role, _ = Role.objects.get_or_create(
            code=role_code,
            defaults={"name": meta["name"], "rank": meta["rank"]},
        )


def assign_all_permissions_to_superuser(user):
    """Provide ALL permissions to a super user of the public schema."""
    public_schema = get_public_schema_name()
    with schema_context(public_schema):
        seed_roles_and_groups_for_schema(is_public=True)

        TenantAccessManager(public_schema).assign_group(user, Role.Code.SUPER_ADMIN)

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

        TenantAccessManager(public_schema).assign_group(user, Role.Code.OPS_MANAGER)

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
    """Assign the tenant-admin group; its permissions are managed through Django."""
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
    access = TenantAccessManager(connection.schema_name)
    access.assign_group(user, Role.Code.TENANT_ADMIN)
    access.grant_all_permissions(Role.Code.TENANT_ADMIN)
    logger.info("Assigned Tenant Admin group to %s in current schema.", user.email)


def assign_public_tenant_owner_role(tenant, public_user):
    """Grant the public owner tenant-scoped role access and Django permissions."""
    from django.contrib.contenttypes.models import ContentType

    public_schema = get_public_schema_name()
    with schema_context(public_schema):
        owner = User.objects.get(email__iexact=public_user.email)
        role, _ = Role.objects.get_or_create(
            code=Role.Code.TENANT_ADMIN,
            defaults={"name": "Tenant Admin", "rank": 100},
        )
        access = TenantAccessManager(public_schema)
        access.assign_group(owner, Role.Code.TENANT_ADMIN)
        access.grant_all_permissions(Role.Code.TENANT_ADMIN)
        tenant_type = ContentType.objects.get_for_model(tenant, for_concrete_model=False)
        assignment, _ = RoleAssignment.objects.get_or_create(
            user=owner,
            role=role,
            scope_type=tenant_type,
            scope_id=tenant.pk,
        )
        logger.info(
            "Assigned public TENANT_ADMIN access to %s for tenant %s.",
            owner.email,
            tenant.schema_name,
        )
        return assignment


def assign_role_default_permissions(user, role_code):
    """Assign the same-named built-in Django group to a user."""
    TenantAccessManager(connection.schema_name).assign_group(user, role_code)


def set_role(user, code, assigned_by=None):
    """Give the user exactly one managed tenant-wide role and its Django group."""
    RoleAssignment.objects.filter(user=user, scope_type__isnull=True, role__code__in=MANAGED_ROLES).delete()
    user.groups.remove(*user.groups.filter(name__in=MANAGED_ROLES))
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
    user, _ = User.objects.get_or_create(
        email=public_user.email.lower(),
        defaults={
            "full_name": public_user.full_name,
            "phone": public_user.phone,
            "is_active": public_user.is_active,
            "password": public_user.password,
        },
    )
    # The public owner record is the source when provisioning or reconciling a
    # tenant. Keep every mirrored profile field current, including values that
    # were previously populated in the tenant schema.
    updated_fields = []
    for field in ("full_name", "phone", "is_active", "password"):
        value = getattr(public_user, field)
        if getattr(user, field) != value:
            setattr(user, field, value)
            updated_fields.append(field)
    if updated_fields:
        user.save(update_fields=updated_fields)

    assign_tenant_admin_permissions(user)
    return user


def sync_owner_profile(user, update_fields=None):
    """Copy shared identity fields between an owner and their schema mirrors.

    The User table is schema-local, so synchronization is deliberately limited
    to users linked to a Tenant as an owner. Tenant-specific users with the
    same email are not affected.
    """
    from apps.platform.models import Tenant

    public_schema = get_public_schema_name()
    current_schema = connection.schema_name
    fields = ("full_name", "phone", "is_active", "password")
    if update_fields is not None:
        fields = tuple(field for field in fields if field in update_fields)
    if not fields:
        return
    if current_schema == public_schema:
        source_email = user.email
        with schema_context(public_schema):
            public_user = User.objects.filter(email__iexact=source_email).first()
            if not public_user:
                return
            tenants = list(Tenant.objects.filter(owners__pk=public_user.pk).exclude(schema_name=public_schema))
    else:
        source_email = user.email
        with schema_context(public_schema):
            public_user = User.objects.filter(email__iexact=source_email).first()
            if not public_user:
                return
            is_owner = Tenant.objects.filter(schema_name=current_schema, owners__pk=public_user.pk).exists()
            if not is_owner:
                return
            tenants = list(Tenant.objects.filter(owners__pk=public_user.pk).exclude(schema_name=public_schema))

    values = {field: getattr(user, field) for field in fields}
    with schema_context(public_schema):
        User.objects.filter(pk=public_user.pk).update(**values)
    for tenant in tenants:
        with schema_context(tenant.schema_name):
            User.objects.filter(email__iexact=source_email).update(**values)


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

        for owner in owners:
            assign_public_tenant_owner_role(tenant, owner)

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

    with schema_context(public_schema):
        if target_schema and target_schema != public_schema:
            tenants = list(Tenant.objects.filter(schema_name=target_schema))
        else:
            tenants = list(Tenant.objects.exclude(schema_name=public_schema))

    for tenant in tenants:
        results[tenant.schema_name] = sync_tenant_roles_and_permissions(tenant)

    return results

