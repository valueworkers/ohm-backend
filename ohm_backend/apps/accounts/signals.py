"""
Signal receivers for the accounts app.

Listens for `tenant_provisioned` to seed the Tenant Admin user inside the
newly-created tenant schema so the workspace owner can log in immediately.
"""
import logging

from django.dispatch import receiver
from django_tenants.utils import schema_context

from apps.platform.services import tenant_provisioned

logger = logging.getLogger(__name__)

# Minimum set of roles every tenant schema needs from day one.
_SEED_ROLES = [
    ("TENANT_ADMIN", "Tenant Admin", 100),
    ("CUSTOMER",     "Customer / User", 10),
]


@receiver(tenant_provisioned)
def seed_tenant_admin(sender, provisioning, tenant, **kwargs):
    """Create the Tenant Admin user inside the new tenant schema.

    The applicant already exists in the *public* schema (created at onboarding
    time with an unusable password).  We replicate the essential fields into the
    tenant schema so that `find_user()` can locate them when they log in through
    the tenant's host / X-Tenant header.
    """
    from apps.accounts.models import Role, RoleAssignment, User

    with schema_context(tenant.schema_name):
        # --- Ensure core roles exist in this schema ---
        for code, name, rank in _SEED_ROLES:
            Role.objects.get_or_create(code=code, defaults={"name": name, "rank": rank})

        applicant_public = provisioning.applicant

        # Don't create a duplicate if this is a retry of a crashed provisioning.
        if User.objects.filter(email__iexact=applicant_public.email).exists():
            logger.info("Tenant admin %s already exists in %s; skipping.", applicant_public.email, tenant.schema_name)
            return

        user = User(
            email=applicant_public.email,
            full_name=applicant_public.full_name,
            phone=applicant_public.phone,
            is_active=True,
        )
        # Copy the password hash so OTP-set or later-set passwords work.
        user.password = applicant_public.password
        user.save()

        # Assign the TENANT_ADMIN role (tenant-wide, scope_type=None).
        role = Role.objects.get(code=Role.Code.TENANT_ADMIN)
        RoleAssignment.objects.get_or_create(user=user, role=role, scope_type=None, scope_id=None)

        logger.info("Seeded tenant admin %s in schema %s.", user.email, tenant.schema_name)
