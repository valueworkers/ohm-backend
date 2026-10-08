from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework_simplejwt.exceptions import AuthenticationFailed

from .models import Tenant, Domain, TenantProvisioning
from .utils import schema_for_slug


def authenticate_root_user(request, *, email, password):
    user = authenticate(request, email=email, password=password)
    if user is None or not user.is_active or not user.is_superuser:
        raise AuthenticationFailed("Invalid root account email or password.")
    return user


def update_onboarding_request(
    request_id,
    *,
    status,
    user,
    review_notes="",
    applicant_message="",
    approved_modules=None,
):
    """Apply one valid root review transition to an onboarding request."""
    transitions = {
        TenantProvisioning.Status.SUBMITTED: {
            TenantProvisioning.Status.IN_REVIEW,
            TenantProvisioning.Status.NEEDS_INFO,
            TenantProvisioning.Status.REJECTED,
        },
        TenantProvisioning.Status.NEEDS_INFO: {
            TenantProvisioning.Status.IN_REVIEW,
            TenantProvisioning.Status.REJECTED,
        },
        TenantProvisioning.Status.IN_REVIEW: {
            TenantProvisioning.Status.NEEDS_INFO,
            TenantProvisioning.Status.APPROVED,
            TenantProvisioning.Status.REJECTED,
        },
    }

    with transaction.atomic():
        onboarding_request = TenantProvisioning.objects.select_for_update().get(pk=request_id)
        if status not in transitions.get(onboarding_request.status, set()):
            raise ValueError(f"Cannot change request status from {onboarding_request.status} to {status}.")
        if status == TenantProvisioning.Status.NEEDS_INFO and not applicant_message.strip():
            raise ValueError("An applicant message is required when requesting information.")
        if status == TenantProvisioning.Status.APPROVED:
            modules = approved_modules or []
            if not set(modules).issubset(onboarding_request.requested_modules):
                raise ValueError("Select only modules requested by the applicant.")
            onboarding_request.approved_modules = modules
        if status == TenantProvisioning.Status.REJECTED:
            onboarding_request.initial_password_hash = ""

        onboarding_request.status = status
        onboarding_request.review_notes = review_notes
        onboarding_request.applicant_message = applicant_message
        onboarding_request.reviewed_by = user
        onboarding_request.reviewed_at = timezone.now()
        onboarding_request.save(update_fields=[
            "status", "review_notes", "applicant_message", "approved_modules", "initial_password_hash",
            "reviewed_by", "reviewed_at", "updated_at",
        ])
        return onboarding_request


def provision_tenant(
    *, name, slug, owner_email, owner_name="", owner_password=None, owner_password_hash="",
    custom_domain=None, modules=None,
):
    """Provision one provider schema and its first tenant admin; roll back on failure."""
    from apps.accounts.models import Role, RoleAssignment, User
    from apps.accounts.seed import seed_roles

    schema = schema_for_slug(slug)
    tenant = None
    custom = None
    with schema_context(get_public_schema_name()):
        try:
            tenant = Tenant(
                schema_name=schema,
                name=name,
                modules=modules or [],
            )
            tenant.save()  # CREATE SCHEMA + migrate tenant apps

            Domain.objects.create(
                domain=f"{slug}.{settings.BASE_DOMAIN}", tenant=tenant,
                is_primary=True, verified=True, verified_at=timezone.now(),
            )
            if custom_domain:
                custom = Domain.objects.create(domain=custom_domain, tenant=tenant, is_primary=False, verified=False)

            with schema_context(schema):
                seed_roles()
                tenant_admin = User.objects.create_user(
                    email=owner_email, password=owner_password, full_name=owner_name
                )
                if owner_password_hash:
                    tenant_admin.password = owner_password_hash
                    tenant_admin.save(update_fields=["password"])
                RoleAssignment.objects.create(user=tenant_admin, role=Role.objects.get(code=Role.Code.TENANT_ADMIN))

            tenant.status = Tenant.Status.ACTIVE
            tenant.save(update_fields=["status"])
        except Exception:
            if tenant is not None and tenant.pk:
                tenant.auto_drop_schema = True
                tenant.delete()
            raise
    return tenant, custom


def provision_approved_request(request_id):
    """Provision an approved request once; requests remain in public schema."""
    with transaction.atomic():
        onboarding_request = TenantProvisioning.objects.select_for_update().get(pk=request_id)
        if onboarding_request.status != TenantProvisioning.Status.APPROVED:
            raise ValueError("Only an approved onboarding request can be provisioned.")
        if not onboarding_request.initial_password_hash:
            raise ValueError("The request has no initial Tenant Admin password.")
        onboarding_request.status = TenantProvisioning.Status.PROVISIONING
        onboarding_request.provision_error = ""
        onboarding_request.save(update_fields=["status", "provision_error", "updated_at"])

    try:
        tenant, _custom_domain = provision_tenant(
            name=onboarding_request.organization_name,
            slug=onboarding_request.slug,
            owner_name=onboarding_request.contact_name,
            owner_email=onboarding_request.contact_email,
            owner_password_hash=onboarding_request.initial_password_hash,
            custom_domain=onboarding_request.custom_domain or None,
            modules=onboarding_request.approved_modules,
        )
    except Exception as exc:
        TenantProvisioning.objects.filter(pk=request_id).update(
            status=TenantProvisioning.Status.APPROVED,
            provision_error=f"{type(exc).__name__}: {exc}"[:2000],
            updated_at=timezone.now(),
        )
        raise

    TenantProvisioning.objects.filter(pk=request_id).update(
        tenant=tenant,
        status=TenantProvisioning.Status.PROVISIONED,
        initial_password_hash="",
        provision_error="",
        updated_at=timezone.now(),
    )
    onboarding_request.refresh_from_db()
    return onboarding_request
