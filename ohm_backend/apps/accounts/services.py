import logging
from django.db import connection
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from .access import TenantAccessManager
from .models import User, UserSession

logger = logging.getLogger(__name__)

MANAGED_ROLES = ("TENANT_ADMIN", "CUSTOMER")

ROLE_GROUP_NAMES = {
    "SUPER_ADMIN": "Super User",
    "TENANT_ADMIN": "Tenant Admin",
    "OPS_MANAGER": "Ops Manager",
    "STAFF": "Staff Member",
}


class AccountsService:
    """Account role, permission, and cross-schema user operations."""

    @classmethod
    def _group_name(cls, role_code):
        """Use the canonical group names created by bootstrap_platform."""
        return ROLE_GROUP_NAMES.get(role_code, role_code)

    @classmethod
    def bootstrap_roles_for_schema(cls, scope):
        """Run the bootstrap command's canonical role/group setup for a schema."""
        from apps.platform.management.commands.bootstrap_platform import Command

        Command().seed_roles(scope)

    @classmethod
    def assign_all_permissions_to_superuser(cls, user):
        """Assign the Super User group in the public schema if it exists."""
        from django.contrib.auth.models import Group

        public_schema = get_public_schema_name()
        with schema_context(public_schema):
            super_group = Group.objects.filter(name=cls._group_name("SUPER_ADMIN")).first()
            if super_group:
                user.groups.add(super_group)

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

    @classmethod
    def assign_default_permissions_to_public_root_user(cls, user):
        """Provide default permissions to a non-superuser public root user (staff/platform admin)."""
        public_schema = get_public_schema_name()
        with schema_context(public_schema):
            TenantAccessManager(public_schema).assign_group(user, cls._group_name("OPS_MANAGER"))

            if not user.is_staff:
                user.is_staff = True
                user.save(update_fields=["is_staff"])

            logger.info("Assigned default root permissions to %s in public schema.", user.email)

    @classmethod
    def assign_tenant_admin_permissions(cls, user, role=None, *, create_role_assignment=True):
        """Assign the bootstrapped Tenant Admin group."""
        access = TenantAccessManager(connection.schema_name)
        group_name = cls._group_name("TENANT_ADMIN")
        access.assign_group(user, group_name)
        logger.info("Assigned Tenant Admin group to %s in current schema.", user.email)

    @classmethod
    def assign_public_tenant_owner_role(cls, tenant, public_user):
        """Grant the public owner Tenant Admin group access."""
        public_schema = get_public_schema_name()
        with schema_context(public_schema):
            owner = User.objects.get(email__iexact=public_user.email)
            access = TenantAccessManager(public_schema)
            group_name = cls._group_name("TENANT_ADMIN")
            access.assign_group(owner, group_name)
            logger.info(
                "Assigned public TENANT_ADMIN access to %s for tenant %s.",
                owner.email,
                tenant.schema_name,
            )
            return None

    @classmethod
    def assign_role_default_permissions(cls, user, role_code):
        """Assign the Django group created by bootstrap for this role."""
        TenantAccessManager(connection.schema_name).assign_group(user, cls._group_name(role_code))

    @classmethod
    def set_role(cls, user, code, assigned_by=None):
        """Give the user exactly one managed tenant-wide group."""
        managed_group_names = [cls._group_name(role_code) for role_code in MANAGED_ROLES] + list(MANAGED_ROLES)
        user.groups.remove(*user.groups.filter(name__in=managed_group_names))
        cls.assign_role_default_permissions(user, code)

    @classmethod
    def ensure_tenant_admin(cls, public_user, *, as_superuser=True):
        """Mirror an OHM user into the current tenant schema as its tenant admin (and superuser)."""
        if as_superuser:
            return cls.ensure_superuser(public_user)

        user, _ = User.objects.get_or_create(
            email=public_user.email.lower(),
            defaults={
                "first_name": public_user.first_name,
                "middle_name": getattr(public_user, "middle_name", ""),
                "last_name": public_user.last_name,
                "mobile_number": public_user.mobile_number,
                "is_active": public_user.is_active,
                "password": public_user.password,
            },
        )
        # The public owner record is the source when provisioning or reconciling a
        # tenant. Keep every mirrored profile field current, including values that
        # were previously populated in the tenant schema.
        updated_fields = []
        for field in ("first_name", "middle_name", "last_name", "mobile_number", "is_active", "password"):
            value = getattr(public_user, field, None)
            if value is not None and getattr(user, field, None) != value:
                setattr(user, field, value)
                updated_fields.append(field)
        if updated_fields:
            user.save(update_fields=updated_fields)

        cls.assign_tenant_admin_permissions(user)
        return user

    @classmethod
    def ensure_superuser(cls, public_user_or_credentials, schema_name=None):
        """Create or mirror a superuser into the specified or current tenant schema.

        Handles user creation using the provided tenant user or credentials,
        maintaining schema context and ensuring idempotency with full superuser
        and tenant admin access.
        """
        if schema_name and connection.schema_name != schema_name:
            with schema_context(schema_name):
                return cls.ensure_superuser(public_user_or_credentials)

        from django.contrib.auth.models import Group

        if isinstance(public_user_or_credentials, dict):
            email = public_user_or_credentials.get("email", "").strip().lower()
            if not email:
                raise ValueError("Email is required to create a superuser.")
            raw_password = public_user_or_credentials.get("password")
            first_name = public_user_or_credentials.get("first_name", "")
            middle_name = public_user_or_credentials.get("middle_name", "")
            last_name = public_user_or_credentials.get("last_name", "")
            mobile_number = public_user_or_credentials.get("mobile_number")
            is_active = public_user_or_credentials.get("is_active", True)
            is_raw_password = bool(raw_password and not raw_password.startswith(("pbkdf2_", "argon2", "bcrypt", "!")))
        else:
            public_user = public_user_or_credentials
            email = public_user.email.lower()
            raw_password = getattr(public_user, "password", None)
            first_name = getattr(public_user, "first_name", "")
            middle_name = getattr(public_user, "middle_name", "")
            last_name = getattr(public_user, "last_name", "")
            mobile_number = getattr(public_user, "mobile_number", None)
            is_active = getattr(public_user, "is_active", True)
            is_raw_password = False

        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "first_name": first_name,
                "middle_name": middle_name,
                "last_name": last_name,
                "mobile_number": mobile_number,
                "is_active": is_active,
                "is_staff": True,
                "is_superuser": True,
                "password": raw_password or "",
            },
        )
        if created and is_raw_password:
            user.set_password(raw_password)
            user.save(update_fields=["password"])

        updated_fields = []
        for field, val in (
            ("first_name", first_name),
            ("middle_name", middle_name),
            ("last_name", last_name),
            ("mobile_number", mobile_number),
        ):
            if val is not None and getattr(user, field, None) != val:
                setattr(user, field, val)
                updated_fields.append(field)

        if raw_password:
            if is_raw_password:
                if not user.check_password(raw_password):
                    user.set_password(raw_password)
                    updated_fields.append("password")
            elif getattr(user, "password", None) != raw_password:
                user.password = raw_password
                updated_fields.append("password")

        for flag in ("is_active", "is_staff", "is_superuser"):
            if not getattr(user, flag, False):
                setattr(user, flag, True)
                updated_fields.append(flag)

        if updated_fields:
            user.save(update_fields=list(set(updated_fields)))

        admin_group, _ = Group.objects.get_or_create(name="Tenant Admin")
        user.groups.add(admin_group)
        super_group = Group.objects.filter(name="Super User").first()
        if super_group:
            user.groups.add(super_group)
        return user

    @classmethod
    def create_tenant_superuser(cls, tenant_or_schema, user_or_credentials):
        """Create or ensure a superuser within the specified tenant schema using provided credentials."""
        schema_name = getattr(tenant_or_schema, "schema_name", tenant_or_schema)
        return cls.ensure_superuser(user_or_credentials, schema_name=schema_name)

    @classmethod
    def sync_owner_profile(cls, user, update_fields=None):
        """Copy shared identity fields between an owner and their schema mirrors.

        The User table is schema-local, so synchronization is deliberately limited
        to users linked to a Tenant as an owner. Tenant-specific users with the
        same email are not affected.
        """
        from apps.platform.models import Tenant

        public_schema = get_public_schema_name()
        current_schema = connection.schema_name
        fields = ("first_name", "middle_name", "last_name", "mobile_number", "is_active", "password")
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
            if "password" in values:
                UserSession.objects.filter(user_id=public_user.pk, revoked_at__isnull=True).update(
                    revoked_at=timezone.now()
                )
        for tenant in tenants:
            with schema_context(tenant.schema_name):
                mirror = User.objects.filter(email__iexact=source_email).first()
                if mirror:
                    User.objects.filter(pk=mirror.pk).update(**values)
                    if "password" in values:
                        UserSession.objects.filter(user=mirror, revoked_at__isnull=True).update(
                            revoked_at=timezone.now()
                        )

    @classmethod
    def sync_owner_email(cls, user, old_email, new_email, verified_at):
        """Apply a verified owner email change to the public identity and its tenant mirrors."""
        from apps.platform.models import Tenant

        public_schema = get_public_schema_name()
        current_schema = connection.schema_name
        with schema_context(public_schema):
            if current_schema == public_schema:
                public_user = User.objects.filter(pk=user.pk).first()
            else:
                public_user = User.objects.filter(email__iexact=old_email).first()
            if not public_user:
                return
            tenants = list(Tenant.objects.filter(owners__pk=public_user.pk).exclude(schema_name=public_schema))
            if current_schema != public_schema and not any(t.schema_name == current_schema for t in tenants):
                return
            if User.objects.filter(email__iexact=new_email).exclude(pk=public_user.pk).exists():
                raise ValueError("The verified email is already assigned to a public account.")
            User.objects.filter(pk=public_user.pk).update(email=new_email, email_verified_at=verified_at)
            UserSession.objects.filter(user=public_user, revoked_at__isnull=True).update(
                revoked_at=timezone.now()
            )

        for tenant in tenants:
            with schema_context(tenant.schema_name):
                if tenant.schema_name == current_schema:
                    mirror = User.objects.filter(pk=user.pk).first()
                else:
                    mirror = User.objects.filter(email__iexact=old_email).first()
                if not mirror:
                    continue
                if User.objects.filter(email__iexact=new_email).exclude(pk=mirror.pk).exists():
                    raise ValueError(f"The verified email is already assigned in workspace {tenant.schema_name}.")
                User.objects.filter(pk=mirror.pk).update(email=new_email, email_verified_at=verified_at)
                UserSession.objects.filter(user=mirror, revoked_at__isnull=True).update(
                    revoked_at=timezone.now()
                )

    @classmethod
    def sync_tenant_roles_and_permissions(cls, tenant):
        """
        For a given Tenant instance:
        1. Mirrors every owner in tenant.owners into the tenant schema as TENANT_ADMIN.
        2. Assigns TENANT_ADMIN role, group, and default permissions to each owner.
        3. Synchronizes groups and permissions for any other existing users in the tenant schema
           who have role assignments.
        Returns dict with summary details: {
            'tenant': tenant.schema_name,
            'assigned_owners': [emails],
            'synced_users': [emails]
        }
        """
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
                cls.assign_public_tenant_owner_role(tenant, owner)

        assigned_owners = []
        synced_users = []

        with schema_context(tenant.schema_name):
            for owner in owners:
                admin_user = cls.ensure_tenant_admin(owner)
                assigned_owners.append(admin_user.email)

            for user in User.objects.all():
                if user.groups.exists():
                    synced_users.append(user.email)

        return {
            "tenant": tenant.schema_name,
            "assigned_owners": assigned_owners,
            "synced_users": list(set(synced_users)),
        }

    @classmethod
    def sync_public_schema_roles_and_permissions(cls):
        """
        Assigns bootstrapped groups to superusers and staff/platform admin users.
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
            for superuser in User.objects.filter(is_superuser=True):
                cls.assign_all_permissions_to_superuser(superuser)
                superusers.append(superuser.email)

            for staff_user in User.objects.filter(is_staff=True, is_superuser=False):
                cls.assign_default_permissions_to_public_root_user(staff_user)
                staff_users.append(staff_user.email)

            for pa in PlatformAdmin.objects.select_related("user"):
                if not pa.user.is_superuser and pa.user.email not in staff_users:
                    cls.assign_default_permissions_to_public_root_user(pa.user)
                    staff_users.append(pa.user.email)

        return {
            "schema": public_schema,
            "superusers": superusers,
            "staff_users": staff_users,
        }

    @classmethod
    def assign_roles_and_permissions_to_existing_tenants_and_owners(cls, target_schema=None):
        """
        Coordinates seeding and assigning roles/permissions across public and tenant schemas.
        """
        from apps.platform.models import Tenant

        public_schema = get_public_schema_name()
        results = {}

        if not target_schema or target_schema == public_schema:
            results[public_schema] = cls.sync_public_schema_roles_and_permissions()

        with schema_context(public_schema):
            if target_schema and target_schema != public_schema:
                tenants = list(Tenant.objects.filter(schema_name=target_schema))
            else:
                tenants = list(Tenant.objects.exclude(schema_name=public_schema))

        for tenant in tenants:
            results[tenant.schema_name] = cls.sync_tenant_roles_and_permissions(tenant)

        return results

