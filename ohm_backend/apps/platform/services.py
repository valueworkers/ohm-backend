import logging

from django.db import transaction
from django.dispatch import Signal
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from apps.platform.tasks import send_tenant_welcome_email_task
from apps.accounts.models import User
from apps.accounts.services import ensure_tenant_admin
from .models import Domain, Subscription, Tenant, TenantProvisioning
from .serializers import base_domain
from .utils import base_domain, schema_for_slug



logger = logging.getLogger(__name__)

# Sent after the tenant schema, domains and subscription exist, but before the
# request is marked provisioned. Product apps hook in here to seed data inside
# the tenant schema (branches, ops admin, tenant admin user). If a receiver
# raises, provisioning is rolled back and can be retried.
#   kwargs: provisioning (TenantProvisioning), tenant (Tenant)
tenant_provisioned = Signal()


class ProvisioningError(Exception):
    pass


def _claim(request_id):
    """APPROVED -> PROVISIONING under a row lock, so two workers can't provision one request."""
    with transaction.atomic():
        p = TenantProvisioning.objects.select_for_update().get(pk=request_id)
        if p.status != TenantProvisioning.Status.APPROVED:
            raise ProvisioningError(f"Request is {p.status}; expected approved.")
        p.status = TenantProvisioning.Status.PROVISIONING
        p.provision_error = ""
        p.save(update_fields=["status", "provision_error", "updated_at"])
    return p


def _assign_owner_role(tenant, applicant):
    """Idempotent: mirror the applicant into the tenant schema and give them TENANT_ADMIN."""
    with schema_context(tenant.schema_name):
        ensure_tenant_admin(applicant)

def _get_or_create_tenant(p):
    """Returns (tenant, created). Creating the Tenant also creates and migrates its schema."""
    schema = schema_for_slug(p.slug)
    existing = Tenant.objects.filter(schema_name=schema).first()
    if existing:
        # Leftover from a crashed attempt of this same request is reusable; anything else is a clash.
        if existing.owners.filter(pk=p.applicant_id).exists():
            return existing, False
        raise ProvisioningError(f"Schema '{schema}' already belongs to another tenant.")

    modules = []
    if p.requested_plan_id:
        modules = list(p.requested_plan.features.filter(is_active=True).values_list("code", flat=True))

    tenant = Tenant(
        schema_name=schema,
        name=p.organization_name,
        status=Tenant.Status.PROVISIONING,  # stays here until the owner completes setup and activates
        modules=modules,
        logo=p.logo.name if p.logo else "",
        contact_email=p.email,
        contact_phone=p.contact_phone,
        gst_number=p.gst_number or None,
    )
    tenant.save()
    tenant.owners.add(p.applicant)
    _assign_owner_role(tenant, p.applicant)
    return tenant, True


def _ensure_domain(tenant, domain, **defaults):
    obj, _ = Domain.objects.get_or_create(domain=domain, defaults={"tenant": tenant, **defaults})
    if obj.tenant_id != tenant.pk:
        raise ProvisioningError(f"Domain '{domain}' already belongs to another tenant.")
    return obj

def _create_domains_and_subscription(p, tenant):
    now = timezone.now()
    # Platform subdomain: we own the DNS, so it is trusted and primary immediately.
    _ensure_domain(tenant, f"{p.slug}.{base_domain()}", is_primary=True, verified=True, verified_at=now)
    # Custom domain: stays unverified until the TXT check (verify_pending_domains) passes.
    if p.custom_domain:
        _ensure_domain(tenant, p.custom_domain, is_primary=False, verified=False)

    if p.requested_plan_id:
        Subscription.objects.get_or_create(
            tenant=tenant,
            plan_id=p.requested_plan_id,
            defaults={
                "status": Subscription.Status.ACTIVE,
                "starts_at": now,
                "billing_email": p.email,
            },
        )


def _rollback_tenant(tenant):
    try:
        tenant.auto_drop_schema = True
        tenant.delete(force_drop=True)  # drops the schema; Domain/Subscription cascade
    except Exception:
        logger.exception("Could not roll back tenant %s; clean up the schema manually.", tenant.schema_name)


def provision_tenant(provisioning):
    """Turn an approved onboarding request into a tenant. Safe to retry after a failure.

    Success: request -> PROVISIONED, Tenant.status stays PROVISIONING (awaiting the owner).
    Failure: request -> APPROVED with provision_error set, partial work rolled back.
    """
    p = _claim(provisioning.pk)
    created_tenant = None
    try:
        with schema_context(get_public_schema_name()):
            tenant, created = _get_or_create_tenant(p)
            if created:
                created_tenant = tenant
            with transaction.atomic():
                _create_domains_and_subscription(p, tenant)
            tenant_provisioned.send(sender=TenantProvisioning, provisioning=p, tenant=tenant)
            with transaction.atomic():
                User.objects.filter(
                    pk=p.applicant_id, is_active=False
                ).update(is_active=True)
                # Also activate any co-owner accounts that were created as stubs.
                co_emails = [e.strip().lower() for e in (p.co_owner_emails or []) if e.strip()]
                if co_emails:
                    User.objects.filter(
                        email__in=co_emails, is_active=False
                    ).update(is_active=True)
                p.tenant = tenant
                p.status = TenantProvisioning.Status.PROVISIONED
                p.provision_error = ""
                p.save(update_fields=["tenant", "status", "provision_error", "updated_at"])
    except Exception as exc:
        logger.exception("Provisioning failed for request %s", p.pk)
        if created_tenant is not None:
            _rollback_tenant(created_tenant)
        TenantProvisioning.objects.filter(pk=p.pk).update(
            status=TenantProvisioning.Status.APPROVED,
            tenant=None,
            provision_error=str(exc)[:2000],
            updated_at=timezone.now(),
        )
        raise ProvisioningError(str(exc)) from exc
    transaction.on_commit(lambda: send_tenant_welcome_email_task.delay(str(p.pk)))
    return tenant
