"""
Signal receivers for the accounts app.

Listens for:
- `tenant_provisioned` to mirror Tenant Admin users into configured tenant schemas.
- `post_save` on `User` to assign all permissions to public superusers
  and default permissions to public root users (staff/platform admins).
- `post_save` on `PlatformAdmin` to ensure platform admins have proper permissions.
"""
import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver
from django.utils import timezone
from django_tenants.signals import post_schema_sync
from django_tenants.utils import get_public_schema_name, schema_context

from apps.accounts.models import User, UserSession
from apps.accounts.services import AccountsService
from apps.platform.models import PlatformAdmin, Tenant
from apps.platform.services import tenant_provisioned

logger = logging.getLogger(__name__)


@receiver(post_schema_sync)
def bootstrap_new_tenant_schema(sender, tenant, **kwargs):
    """Set up a tenant schema using the bootstrap command's canonical roles."""
    schema_name = tenant.get("schema_name") if isinstance(tenant, dict) else getattr(tenant, "schema_name", None)
    if schema_name and schema_name != get_public_schema_name():
        with schema_context(schema_name):
            AccountsService.bootstrap_roles_for_schema("tenant")


def _get_or_create_public_user(email):
    """Return the public-schema User for an email, creating a stub if needed."""
    PublicUser = get_user_model()
    with schema_context(get_public_schema_name()):
        user = PublicUser.objects.filter(email__iexact=email).first()
        if user:
            return user
        try:
            with transaction.atomic():
                user = PublicUser(email=email.lower(), is_active=False)
                user.set_unusable_password()
                user.save()
        except IntegrityError:
            user = PublicUser.objects.get(email__iexact=email)
        return user


@receiver(tenant_provisioned)
def seed_tenant_admin(sender, provisioning, tenant, **kwargs):
    """Mirror tenant owners and create superuser into a schema whose roles and groups were bootstrapped."""
    # Resolve the applicant while the public schema is active. The applicant
    # row does not exist in the tenant schema until mirrored.
    public_schema = get_public_schema_name()
    with schema_context(public_schema):
        applicant = User.objects.get(pk=provisioning.applicant_id)

    # Only identities named by this onboarding request belong in the tenant.
    with schema_context(tenant.schema_name):
        AccountsService.ensure_superuser(applicant)

    # Seed co-owners as Tenant Admins / superusers
    co_emails = provisioning.co_owner_emails or []
    for email in co_emails:
        email = email.strip().lower()
        if not email or email == applicant.email.lower():
            continue
        public_user = _get_or_create_public_user(email)
        # Add as tenant owner in the public schema
        with schema_context(get_public_schema_name()):
            tenant.owners.add(public_user)
        # Seed in the tenant schema
        with schema_context(tenant.schema_name):
            AccountsService.ensure_superuser(public_user)

    # The onboarding form may name one operations administrator. Mirror that
    # identity with the tenant's operational role, never with platform-wide
    # credentials. If the email is already an owner, keep its owner privileges.
    ops_email = (provisioning.ops_admin_email or "").strip().lower()
    owner_emails = {applicant.email.strip().lower(), *(e.strip().lower() for e in co_emails if e.strip())}
    if ops_email and ops_email not in owner_emails:
        ops_user = _get_or_create_public_user(ops_email)
        with schema_context(tenant.schema_name):
            tenant_ops_user = AccountsService.ensure_tenant_admin(ops_user, as_superuser=False)
            AccountsService.set_role(tenant_ops_user, "OPS_MANAGER")


@receiver(m2m_changed, sender=Tenant.owners.through)
@transaction.atomic
def on_tenant_owners_changed(sender, instance, action, pk_set, **kwargs):
    """Mirror owner grants and revoke tenant-admin access when ownership is removed."""
    if action == "pre_clear":
        with schema_context(get_public_schema_name()):
            instance._owners_cleared_ids = list(instance.owners.values_list("pk", flat=True))
        return
    if action == "post_clear":
        pk_set = set(getattr(instance, "_owners_cleared_ids", []))
        instance._owners_cleared_ids = []
        action = "post_remove"
    if action in {"post_remove", "post_add"} and pk_set:
        tenant = instance
        public_schema = get_public_schema_name()
        if tenant.schema_name == public_schema:
            return
        if action == "post_add":
            with schema_context(public_schema):
                users = list(User.objects.filter(pk__in=pk_set))
                for public_user in users:
                    AccountsService.assign_public_tenant_owner_role(tenant, public_user)
            with schema_context(tenant.schema_name):
                for public_user in users:
                    AccountsService.ensure_tenant_admin(public_user)
            return

        with schema_context(public_schema):
            users = list(User.objects.filter(pk__in=pk_set))
        with schema_context(tenant.schema_name):
            for public_user in users:
                mirror = User.objects.filter(email__iexact=public_user.email).first()
                if not mirror:
                    continue
                tenant_admin_groups = mirror.groups.filter(name__in=["Tenant Admin", "TENANT_ADMIN"])
                if tenant_admin_groups.exists():
                    mirror.groups.remove(*tenant_admin_groups)
                if not mirror.groups.exists() and not mirror.is_superuser:
                    mirror.is_active = False
                    mirror.save(update_fields=["is_active"])
                    UserSession.objects.filter(user=mirror, revoked_at__isnull=True).update(
                        revoked_at=timezone.now()
                    )


@receiver(post_save, sender=User)
def on_user_saved(sender, instance, created, **kwargs):
    """Maintain platform permissions and synchronize tenant-owner identity."""
    if kwargs.get("raw", False):
        return

    public_schema = get_public_schema_name()
    current_schema = getattr(connection, "schema_name", None)

    # Only public schema users are platform superusers / root users
    if current_schema == public_schema:
        if instance.is_superuser:
            # For super user of public schema provide all permissions
            AccountsService.assign_all_permissions_to_superuser(instance)
        elif instance.is_staff or hasattr(instance, "platform_admin_profile"):
            # When creating public root user provide some default permissions
            AccountsService.assign_default_permissions_to_public_root_user(instance)

    # Mirror only shared identity fields for registered tenant owners. The
    # service uses queryset updates in the other schemas, avoiding recursive
    # post_save signals and leaving tenant-specific roles untouched.
    AccountsService.sync_owner_profile(instance, update_fields=kwargs.get("update_fields"))


@receiver(post_save, sender=PlatformAdmin)
def on_platform_admin_saved(sender, instance, created, **kwargs):
    """Ensure platform admin users have default or all permissions in the public schema."""
    if kwargs.get("raw", False):
        return

    user = instance.user
    if user.is_superuser:
        AccountsService.assign_all_permissions_to_superuser(user)
    else:
        AccountsService.assign_default_permissions_to_public_root_user(user)
