"""
apps/care/models.py
───────────────────
Care Product Module Models.

REUSES the existing core clinical, order, and catalogue models from `apps.billing.models`:
  • Patient, PatientDocument, PatientDocumentFile
  • Location, Package
  • PrimaryOrder, SecondaryOrder, TernaryOrder
  • ContactBooking

Defines only Care-specific product extensions:
  • SoftDeleteModel (abstract base mixin)
  • TenantPaymentConfig (per-tenant Razorpay payment credentials)
  • AttendanceStatus (attendance status codes)
  • Attendance (staff attendance logs)
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from .constants import AttendanceStatusCode

# Re-export existing core models from apps.billing.models so apps.care uses
# the single source of truth without duplicating tables or model definitions:
from apps.billing.models import (  # noqa: F401
    ContactBooking,
    Location,
    Package,
    Patient,
    PatientDocument,
    PatientDocumentFile,
    PrimaryOrder,
    SecondaryOrder,
    TernaryOrder,
)


# ---------------------------------------------------------------------------
# Soft Delete Mixin (for Care-specific models)
# ---------------------------------------------------------------------------

class SoftDeleteModel(models.Model):
    """Abstract base model for soft deletion with status/active management."""

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        update_fields = ["is_deleted", "deleted_at"]
        if hasattr(self, "status") and isinstance(self._meta.get_field("status"), models.BooleanField):
            self.status = False
            update_fields.append("status")
        elif hasattr(self, "is_active") and isinstance(self._meta.get_field("is_active"), models.BooleanField):
            self.is_active = False
            update_fields.append("is_active")
        self.save(update_fields=update_fields)

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        update_fields = ["is_deleted", "deleted_at"]
        if hasattr(self, "status") and isinstance(self._meta.get_field("status"), models.BooleanField):
            self.status = True
            update_fields.append("status")
        elif hasattr(self, "is_active") and isinstance(self._meta.get_field("is_active"), models.BooleanField):
            self.is_active = True
            update_fields.append("is_active")
        self.save(update_fields=update_fields)


# ---------------------------------------------------------------------------
# Per-Tenant Gateway Configuration (Razorpay)
# ---------------------------------------------------------------------------

class TenantPaymentConfig(SoftDeleteModel):
    """Stores per-tenant Razorpay payment gateway credentials."""

    razorpay_key_id = models.CharField(max_length=120)
    razorpay_key_secret = models.CharField(max_length=120)
    razorpay_webhook_secret = models.CharField(max_length=120, blank=True)
    currency = models.CharField(max_length=10, default="INR")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "care_tenantpaymentconfig"
        verbose_name = "Tenant Payment Configuration"
        verbose_name_plural = "Tenant Payment Configurations"

    def __str__(self) -> str:
        masked = f"{self.razorpay_key_id[:6]}..." if len(self.razorpay_key_id) > 6 else self.razorpay_key_id
        return f"Razorpay Config ({masked})"


# ---------------------------------------------------------------------------
# Staff Attendance (for mark_attendance_present task)
# ---------------------------------------------------------------------------

class AttendanceStatus(SoftDeleteModel):
    code = models.CharField(max_length=30, unique=True, default=AttendanceStatusCode.PRESENT)
    label = models.CharField(max_length=100)

    class Meta:
        db_table = "care_attendancestatus"
        verbose_name = "Attendance Status"
        verbose_name_plural = "Attendance Statuses"

    def __str__(self) -> str:
        return f"{self.label} ({self.code})"


class Attendance(SoftDeleteModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="care_attendances",
    )
    date = models.DateField(db_index=True)
    status_record = models.ForeignKey(
        AttendanceStatus,
        on_delete=models.PROTECT,
        related_name="attendances",
    )
    duration = models.DurationField(null=True, blank=True)
    reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "care_attendance"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "date"],
                name="care_unique_user_date_attendance",
            )
        ]
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"{self.user} - {self.date} ({self.status_record.code})"
