import secrets
import uuid

from django.conf import settings
from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


def _verification_token():
    return secrets.token_hex(16)


class Tenant(TenantMixin):
    """A healthcare service provider workspace, managed by OHM in the public schema."""

    class Status(models.TextChoices):
        PROVISIONING = "provisioning"
        ACTIVE = "active"
        SUSPENDED = "suspended"

    name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROVISIONING)
    modules = models.JSONField(default=list, blank=True)  # enabled product modules, e.g. ["patients", "booking"]
    created_at = models.DateTimeField(auto_now_add=True)

    auto_create_schema = True   # create + migrate schema on first save
    auto_drop_schema = False    # never drop data by accident; services.py opts in on rollback

    def __str__(self):
        return f"{self.name} ({self.schema_name})"


class Feature(models.Model):
    """A feature module that OHM can enable for service-provider tenants."""

    code = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Subscription(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="subscriptions")
    status = models.CharField(max_length=20, default="active")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    features = models.ManyToManyField(Feature, blank=True, related_name="subscriptions")
    created_at = models.DateTimeField(auto_now_add=True)


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
    """Provider application stored in public until an OHM admin provisions it."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        IN_REVIEW = "in_review", "In Review"
        NEEDS_INFO = "needs_info", "Needs Information"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        PROVISIONING = "provisioning", "Provisioning"
        PROVISIONED = "provisioned", "Provisioned"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contact_name = models.CharField(max_length=200)
    contact_email = models.EmailField()
    contact_phone = models.CharField(max_length=30, blank=True)
    organization_name = models.CharField(max_length=200)
    organization_type = models.CharField(max_length=120, blank=True)
    organization_address = models.JSONField(default=dict, blank=True)
    slug = models.SlugField(max_length=30)
    custom_domain = models.CharField(max_length=253, blank=True)
    requested_modules = models.JSONField(default=list, blank=True)
    initial_password_hash = models.CharField(max_length=256, blank=True)
    approved_modules = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED)
    review_notes = models.TextField(blank=True)
    applicant_message = models.TextField(blank=True)
    access_token_hash = models.CharField(max_length=64)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_onboarding_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.SET_NULL, related_name="provisioning_requests")
    provision_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.organization_name} ({self.status})"


class PlatformAdmin(models.Model):
    """Public-schema platform privileges linked to the shared auth user model."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="platform_admin_profile")
    title = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.user.email
