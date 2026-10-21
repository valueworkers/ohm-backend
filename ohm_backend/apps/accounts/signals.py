"""
Signal receivers for the accounts app.

Listens for:
- `tenant_provisioned` and `post_schema_sync` to seed default permissions,
  roles, groups, and Tenant Admin users inside newly-created tenant schemas.
- `post_save` on `User` to assign all permissions to public superusers
  and default permissions to public root users (staff/platform admins).
- `post_save` on `PlatformAdmin` to ensure platform admins have proper permissions.
"""
import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver
from django_tenants.signals import post_schema_sync
from django_tenants.utils import get_public_schema_name, schema_context

from apps.accounts.models import User
from apps.accounts.services import (
    assign_all_permissions_to_superuser,
    assign_default_permissions_to_public_root_user,
    ensure_tenant_admin,
    seed_tenant_default_permissions,
)
from apps.platform.models import PlatformAdmin, Tenant
from apps.platform.services import tenant_provisioned

logger = logging.getLogger(__name__)


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


@receiver(post_schema_sync)
def on_tenant_schema_synced(sender, tenant, **kwargs):
    """Whenever a tenant schema is synced/migrated, seed its default permissions and roles."""
    schema_name = tenant.get("schema_name") if isinstance(tenant, dict) else getattr(tenant, "schema_name", None)
    if schema_name and schema_name != get_public_schema_name():
        seed_tenant_default_permissions(schema_name)


@receiver(tenant_provisioned)
def seed_tenant_admin(sender, provisioning, tenant, **kwargs):
    """Create default permissions and the Tenant Admin user inside the new tenant schema."""
    # Seed default permissions, roles, and groups for this tenant
    seed_tenant_default_permissions(tenant.schema_name)

    # Seed primary applicant with full tenant admin default permissions
    with schema_context(tenant.schema_name):
        ensure_tenant_admin(provisioning.applicant)

    # Seed co-owners
    co_emails = provisioning.co_owner_emails or []
    for email in co_emails:
        email = email.strip().lower()
        if not email or email == provisioning.applicant.email.lower():
            continue
        public_user = _get_or_create_public_user(email)
        # Add as tenant owner in the public schema
        with schema_context(get_public_schema_name()):
            tenant.owners.add(public_user)
        # Seed as TENANT_ADMIN in the tenant schema
        with schema_context(tenant.schema_name):
            ensure_tenant_admin(public_user)


@receiver(m2m_changed, sender=Tenant.owners.through)
def on_tenant_owners_changed(sender, instance, action, pk_set, **kwargs):
    """Automatically mirror newly-added owners into the tenant schema as TENANT_ADMIN."""
    if action == "post_add" and pk_set:
        tenant = instance
        public_schema = get_public_schema_name()
        if tenant.schema_name == public_schema:
            return
        users = User.objects.filter(pk__in=pk_set)
        with schema_context(tenant.schema_name):
            for u in users:
                ensure_tenant_admin(u)


@receiver(post_save, sender=User)
def on_user_saved(sender, instance, created, **kwargs):
    """Ensure superusers of public schema have ALL permissions, and public root users have default permissions."""
    if kwargs.get("raw", False):
        return

    public_schema = get_public_schema_name()
    current_schema = getattr(connection, "schema_name", None)

    # Only public schema users are platform superusers / root users
    if current_schema == public_schema:
        if instance.is_superuser:
            # For super user of public schema provide all permissions
            assign_all_permissions_to_superuser(instance)
        elif instance.is_staff or hasattr(instance, "platform_admin_profile"):
            # When creating public root user provide some default permissions
            assign_default_permissions_to_public_root_user(instance)


@receiver(post_save, sender=PlatformAdmin)
def on_platform_admin_saved(sender, instance, created, **kwargs):
    """Ensure platform admin users have default or all permissions in the public schema."""
    if kwargs.get("raw", False):
        return

    user = instance.user
    if user.is_superuser:
        assign_all_permissions_to_superuser(user)
    else:
        assign_default_permissions_to_public_root_user(user)
