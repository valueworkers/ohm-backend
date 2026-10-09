import secrets
import uuid

from django.conf import settings
from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


def _verification_token():
    return secrets.token_hex(16)


class Feature(models.Model):
    """A feature module that OHM can enable for service-provider tenants."""

    code = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Plan(models.Model):
    """A subscription plan. Limits and bundled features live here, not on Tenant."""

    code = models.SlugField(max_length=50, unique=True)  # e.g. basic, professional
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    features = models.ManyToManyField(Feature, blank=True, related_name="plans")
    max_employees = models.PositiveIntegerField(null=True, blank=True)  # null = unlimited
    max_branches = models.PositiveIntegerField(null=True, blank=True)   # null = unlimited
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Tenant(TenantMixin):
    """A healthcare service provider workspace, managed by OHM in the public schema."""

    class Status(models.TextChoices):
        PROVISIONING = "provisioning"
        ACTIVE = "active"
        SUSPENDED = "suspended"

    name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROVISIONING)
    modules = models.JSONField(default=list, blank=True)  # enabled product modules, e.g. ["patients", "booking"]
    # The user who requested the tenant; manages its profile and activates it.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="owned_tenants"
    )

    # --- Organization profile (copied from the approved onboarding request) ---
    logo = models.ImageField(upload_to="tenants/logos/", null=True, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=30, blank=True)
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    default_currency = models.CharField(max_length=3, default="INR")

    created_at = models.DateTimeField(auto_now_add=True)

    auto_create_schema = True   # create + migrate schema on first save
    auto_drop_schema = False    # never drop data by accident; services.py opts in on rollback

    def __str__(self):
        return f"{self.name} ({self.schema_name})"


class Subscription(models.Model):
    class Status(models.TextChoices):
        TRIAL = "trial", "Trial"
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        SUSPENDED = "suspended", "Suspended"

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="subscriptions")
    plan = models.ForeignKey(Plan, null=True, blank=True, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    billing_email = models.EmailField(blank=True)
    # Extra features on top of the plan's bundle (per-tenant add-ons).
    features = models.ManyToManyField(Feature, blank=True, related_name="subscriptions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self):
        return f"{self.tenant} - {self.plan or 'no plan'} ({self.status})"


class Domain(DomainMixin):
    """A provider hostname that routes requests to its tenant schema when verified."""

    verified = models.BooleanField(default=False)
    verification_token = models.CharField(max_length=64, default=_verification_token, editable=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        self.domain = self.domain.strip().lower()
        super().save(*args, **kwargs)


class TenantProvisioning(models.Model):
    """Provider application stored in public until a root admin approves it."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        PROVISIONING = "provisioning", "Provisioning"
        PROVISIONED = "provisioned", "Provisioned"

    OPEN_STATUSES = ["submitted", "approved", "provisioning"]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    applicant = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="onboarding_requests"
    )

    # --- Fields from the onboarding form ---
    organization_name = models.CharField(max_length=200)          # Tenant name
    logo = models.ImageField(upload_to="onboarding/logos/", null=True, blank=True)
    email = models.EmailField()                                   # Email
    contact_phone = models.CharField(max_length=30)               # Phone number
    address = models.TextField(blank=True)                        # Organization address
    branches = models.JSONField(
        default=list, blank=True,
        help_text='List of branches: [{"name": "...", "address": "..."}]',
    )
    ops_admin_email = models.EmailField(blank=True)               # blank = no ops admin
    slug = models.SlugField(max_length=30)                        # Subdomain -> slug.o-hm.com
    custom_domain = models.CharField(max_length=253, blank=True)  # blank = no own domain
    wants_custom_website = models.BooleanField(default=False)     # Customized website
    requested_plan = models.ForeignKey(
        Plan, null=True, blank=True, on_delete=models.SET_NULL, related_name="onboarding_requests"
    )

    # --- Workflow fields (not on the form, needed for the lobby/approval flow) ---
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED)
    review_notes = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_onboarding_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, on_delete=models.SET_NULL, related_name="provisioning_requests"
    )
    provision_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["slug"], name="uniq_open_request_slug",
                condition=models.Q(status__in=["submitted", "approved", "provisioning"]),
            ),
            models.UniqueConstraint(
                fields=["custom_domain"], name="uniq_open_request_domain",
                condition=models.Q(status__in=["submitted", "approved", "provisioning"]) & ~models.Q(custom_domain=""),
            ),
        ]

    def __str__(self):
        return f"{self.organization_name} ({self.status})"


class PlatformAdmin(models.Model):
    """Public-schema platform privileges linked to the shared auth user model."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="platform_admin_profile")
    title = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.user.email