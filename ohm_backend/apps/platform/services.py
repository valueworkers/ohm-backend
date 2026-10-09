
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from .models import Tenant, Domain, TenantProvisioning, Feature, Subscription
from .utils import schema_for_slug

logger = logging.getLogger(__name__)

PROVISION_STALE_AFTER = timedelta(minutes=15)

S = TenantProvisioning.Status

REVIEW_TRANSITIONS = {S.SUBMITTED: {S.APPROVED, S.REJECTED}}


def review_onboarding_request(request_id, *, status, user, review_notes=None, approved_modules=None):
    """Root approves or rejects. Approving queues tenant provisioning.

    Re-approving an APPROVED request that has a provision_error re-queues it (retry).
    """
    with transaction.atomic():
        req = TenantProvisioning.objects.select_for_update().get(pk=request_id)
        is_retry = status == S.APPROVED and req.status == S.APPROVED and bool(req.provision_error)
        if not is_retry and status not in REVIEW_TRANSITIONS.get(req.status, set()):
            raise ValueError(f"Cannot change request status from {req.status} to {status}.")

        if status == S.APPROVED and not is_retry:
            modules = list(req.requested_modules) if approved_modules is None else approved_modules
            if not modules:
                raise ValueError("Approve at least one module.")
            if not set(modules).issubset(req.requested_modules):
                raise ValueError("Select only modules requested by the applicant.")
            req.approved_modules = modules

        req.status = status
        if review_notes is not None:
            req.review_notes = review_notes
        req.reviewed_by = user
        req.reviewed_at = timezone.now()
        req.save(update_fields=[
            "status", "review_notes", "approved_modules", "reviewed_by", "reviewed_at", "updated_at",
        ])

        if status == S.APPROVED:
            from .tasks import provision_onboarding_request

            pk = str(req.pk)
            transaction.on_commit(lambda: provision_onboarding_request.delay(pk))
        return req

def provision_tenant(
    *,
    name,
    slug,
    owner_email,
    owner_name="",
    owner_password=None,
    owner_password_hash="",
    custom_domain=None,
    modules=None,
):
    """Provision one provider schema and its first tenant admin; roll back on failure."""
    from apps.accounts.models import Role, RoleAssignment, User
    from apps.accounts.seed import seed_roles

    modules = modules or []
    schema = schema_for_slug(slug)
    tenant = None
    custom = None
    with schema_context(get_public_schema_name()):
        try:
            tenant = Tenant(schema_name=schema, name=name, modules=modules)
            tenant.save()  # CREATE SCHEMA + migrate tenant apps

            Domain.objects.create(
                domain=f"{slug}.{settings.BASE_DOMAIN}", tenant=tenant,
                is_primary=True, verified=True, verified_at=timezone.now(),
            )
            if custom_domain:
                custom = Domain.objects.create(
                    domain=custom_domain, tenant=tenant, is_primary=False, verified=False
                )

            subscription = Subscription.objects.create(tenant=tenant, starts_at=timezone.now())
            subscription.features.set(Feature.objects.filter(code__in=modules, is_active=True))

            with schema_context(schema):
                seed_roles()
                tenant_admin = User.objects.create_user(
                    email=owner_email, password=owner_password, full_name=owner_name
                )
                if owner_password_hash:
                    tenant_admin.password = owner_password_hash
                    tenant_admin.save(update_fields=["password"])
                RoleAssignment.objects.create(
                    user=tenant_admin, role=Role.objects.get(code=Role.Code.TENANT_ADMIN)
                )

            tenant.status = Tenant.Status.ACTIVE
            tenant.save(update_fields=["status"])
        except Exception:
            if tenant is not None and tenant.pk:
                tenant.auto_drop_schema = True
                tenant.delete()
            raise
    return tenant, custom

def provision_approved_request(request_id):
    """Provision an approved request once. Safe to call again after a failure or crash."""
    with transaction.atomic():
        req = (
            TenantProvisioning.objects.select_for_update(of=("self",))
            .select_related("applicant")
            .get(pk=request_id)
        )
        stale = req.status == S.PROVISIONING and req.updated_at < timezone.now() - PROVISION_STALE_AFTER
        if req.status != S.APPROVED and not stale:
            raise ValueError("Only an approved onboarding request can be provisioned.")
        req.status = S.PROVISIONING
        req.provision_error = ""
        req.save(update_fields=["status", "provision_error", "updated_at"])

    try:
        tenant, _custom = provision_tenant(
            name=req.organization_name,
            slug=req.slug,
            owner_name=req.applicant.full_name,
            owner_email=req.applicant.email,
            owner_password_hash=req.applicant.password,  # same credentials the applicant signed up with
            custom_domain=req.custom_domain or None,
            modules=req.approved_modules,
        )
    except Exception as exc:
        TenantProvisioning.objects.filter(pk=request_id).update(
            status=S.APPROVED,
            provision_error=f"{type(exc).__name__}: {exc}"[:2000],
            updated_at=timezone.now(),
        )
        raise

    TenantProvisioning.objects.filter(pk=request_id).update(
        tenant=tenant, status=S.PROVISIONED, provision_error="", updated_at=timezone.now(),
    )
    req.refresh_from_db()
    return req