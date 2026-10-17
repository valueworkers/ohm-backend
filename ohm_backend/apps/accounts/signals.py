"""
Signal receivers for the accounts app.

Listens for `tenant_provisioned` to seed the Tenant Admin user inside the
newly-created tenant schema so the workspace owner can log in immediately.
"""
import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.dispatch import receiver
from django_tenants.utils import get_public_schema_name, schema_context

from apps.platform.services import tenant_provisioned

logger = logging.getLogger(__name__)

# Minimum set of roles every tenant schema needs from day one.
_SEED_ROLES = [
    ("TENANT_ADMIN", "Tenant Admin", 100),
    ("CUSTOMER",     "Customer / User", 10),
]


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


def _seed_user_in_schema(schema_name, public_user, *, role_code="TENANT_ADMIN"):
    """Create or skip a user + role assignment inside a tenant schema."""
    from apps.accounts.models import Role, RoleAssignment, User

    with schema_context(schema_name):
        if User.objects.filter(email__iexact=public_user.email).exists():
            logger.info("User %s already exists in %s; skipping.", public_user.email, schema_name)
            return

        user = User(
            email=public_user.email,
            full_name=public_user.full_name,
            phone=public_user.phone,
            is_active=True,
        )
        # Copy the password hash so OTP-set or later-set passwords work.
        user.password = public_user.password
        user.save()

        role = Role.objects.get(code=role_code)
        RoleAssignment.objects.get_or_create(user=user, role=role, scope_type=None, scope_id=None)
        logger.info("Seeded %s (%s) in schema %s.", user.email, role_code, schema_name)


@receiver(tenant_provisioned)
def seed_tenant_admin(sender, provisioning, tenant, **kwargs):
    """Create the Tenant Admin user inside the new tenant schema.

    The applicant already exists in the *public* schema (created at onboarding
    time with an unusable password).  We replicate the essential fields into the
    tenant schema so that `find_user()` can locate them when they log in through
    the tenant's host / X-Tenant header.

    Co-owners listed in `co_owner_emails` are also seeded as TENANT_ADMIN with
    the same level of access as the primary applicant.
    """
    from apps.accounts.models import Role

    with schema_context(tenant.schema_name):
        # --- Ensure core roles exist in this schema ---
        for code, name, rank in _SEED_ROLES:
            Role.objects.get_or_create(code=code, defaults={"name": name, "rank": rank})

    # --- Seed primary applicant ---
    _seed_user_in_schema(tenant.schema_name, provisioning.applicant)

    # --- Seed co-owners ---
    co_emails = provisioning.co_owner_emails or []
    for email in co_emails:
        email = email.strip().lower()
        if not email or email == provisioning.applicant.email.lower():
            continue
        public_user = _get_or_create_public_user(email)
        # Add as tenant owner in the public schema.
        with schema_context(get_public_schema_name()):
            tenant.owners.add(public_user)
        # Seed as TENANT_ADMIN in the tenant schema.
        _seed_user_in_schema(tenant.schema_name, public_user)
