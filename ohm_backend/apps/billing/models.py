"""
apps/billing/models.py
──────────────────────
Unified billing, quotation, order, and invoice models for OHM tenants.

Features:
  • Patient registration and medical document management
  • Booking orders (PrimaryOrder, SecondaryOrder, TernaryOrder, ContactBooking)
  • Quotations (Quote, QuoteLine, QuoteEvent, QuoteTaxSettings, QuoteCounter)
  • Merged Invoices (Invoice, InvoiceItem, InvoiceSequence, DocumentSequence)
    - Independent quotation invoices
    - Invoices for Venue, Service, Resource
    - Invoices for patient booking orders (Secondary / Ternary)
    - Itemized GST calculations (HSN/SAC, CGST, SGST, IGST)
    - Email sending support (`send_on_mail`)
  • Payments (Payment) with verification and gateway metadata
"""

from __future__ import annotations

import secrets
from secrets import token_urlsafe
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.contenttypes import fields as ct_fields
from django.contrib.contenttypes import models as ct_models
from django.core.exceptions import ValidationError
from django.core.validators import (
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models, transaction
from django.db.models import Count, Exists, Max, Min, OuterRef, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .constants import (
    BookingEntity,
    BookingStatus,
    BookingType,
    InvoiceLineType,
    InvoiceStatus,
    InvoiceType,
    PaymentMappingStatus,
    PaymentMethod,
    PeriodChoices,
    QuoteLineKind,
    QuoteStatus,
    QuoteSupplyType,
    Uom,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def calculate_amount(startdate, enddate, package) -> Decimal:
    """Return the billable amount for a date range given a package pricing rule."""
    if not startdate or not enddate:
        raise ValueError("Start date and end date are required")
    if enddate < startdate:
        raise ValueError("End date must be >= start date")

    if package.period == PeriodChoices.DAILY:
        delta = relativedelta(enddate.date(), startdate.date())
        total_days = delta.days + 1
        return Decimal(total_days) * package.price

    if package.period == PeriodChoices.HOURLY:
        delta = relativedelta(enddate, startdate)
        total_hours = (
            delta.days * 24
            + delta.hours
            + (1 if delta.minutes > 0 or delta.seconds > 0 else 0)
        )
        if total_hours == 0:
            total_hours = 1
        return Decimal(total_hours) * package.price

    raise ValueError(f"Unsupported package period: {package.period!r}")


def auto_update_status(start_datetime, end_datetime) -> str:
    """Derive a time-based BookingStatus from the current moment."""
    now = timezone.now()
    if now < start_datetime:
        return BookingStatus.YET_TO_START
    if start_datetime <= now <= end_datetime:
        return BookingStatus.IN_PROGRESS
    if now > end_datetime:
        return BookingStatus.FULFILLED
    return BookingStatus.LOBBY


def sync_patient_active_status() -> dict[str, int]:
    """
    Set Patient.is_active = True for patients who have at least one
    PrimaryOrder that is currently active.
    """
    active_statuses = [
        BookingStatus.LOBBY,
        BookingStatus.HOLD,
        BookingStatus.BOOKED,
        BookingStatus.DELAYED,
        BookingStatus.YET_TO_START,
        BookingStatus.IN_PROGRESS,
        BookingStatus.PARTIALLY_FULFILLED,
        BookingStatus.MODIFIED,
        BookingStatus.RESCHEDULED,
    ]
    has_active_order = PrimaryOrder.objects.filter(
        patient=OuterRef("pk"),
        status__in=active_statuses,
    )
    patients = Patient.objects.filter(is_deleted=False).annotate(
        has_active_order=Exists(has_active_order)
    )
    to_activate = list(
        patients.filter(has_active_order=True, is_active=False).values_list("pk", flat=True)
    )
    to_deactivate = list(
        patients.filter(has_active_order=False, is_active=True).values_list("pk", flat=True)
    )
    if to_activate:
        Patient.objects.filter(pk__in=to_activate).update(is_active=True)
    if to_deactivate:
        Patient.objects.filter(pk__in=to_deactivate).update(is_active=False)
    return {"activated": len(to_activate), "deactivated": len(to_deactivate)}


def bulk_update_status(queryset, model) -> int:
    """Recompute time-based status for every unlocked order in queryset."""
    orders = queryset.filter(status_locked=False).exclude(
        status__in=[BookingStatus.CANCELLED, BookingStatus.LOBBY]
    )
    to_update = []
    for order in orders:
        new_status = auto_update_status(order.start_datetime, order.end_datetime)
        if new_status != order.status:
            order.status = new_status
            to_update.append(order)
    if to_update:
        model.objects.bulk_update(to_update, ["status"])
    return len(to_update)


def generate_order_id(instance) -> str:
    """Generate a hierarchical order ID: #PPP000000 / #PPPSSS000 / #PPPSSSTTT."""
    if not instance.id:
        raise ValueError("Instance must be saved before generating order_id")
    if hasattr(instance, "secondary_order") and instance.secondary_order:
        secondary = instance.secondary_order
        primary = secondary.primary_order
        return f"#{primary.id:03}{secondary.id:03}{instance.id:03}"
    if hasattr(instance, "primary_order") and instance.primary_order:
        return f"#{instance.primary_order.id:03}{instance.id:03}000"
    return f"#{instance.id:03}000000"


# ---------------------------------------------------------------------------
# Sequences & Counters
# ---------------------------------------------------------------------------

class DocumentSequence(models.Model):
    """Transactional per-prefix/year counters for quotation and invoice numbers."""

    prefix = models.CharField(max_length=8)
    year = models.PositiveSmallIntegerField()
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prefix", "year"],
                name="billing_uniq_doc_seq_prefix_year",
            )
        ]

    def __str__(self) -> str:
        return f"{self.prefix}-{self.year}: {self.last_value}"


class InvoiceSequence(models.Model):
    """Gap-free invoice numbering per calendar year."""

    year = models.PositiveIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.year}: {self.last_number}"


class QuoteCounter(models.Model):
    """Monthly sequence for quote numbers (e.g. YYMM)."""

    period = models.CharField(max_length=4, unique=True)
    last_seq = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.period}: {self.last_seq}"


class QuoteTaxSettings(models.Model):
    """Seller GST identity configured per tenant workspace."""

    gstin = models.CharField(max_length=15, blank=True)
    state_code = models.CharField(max_length=2, blank=True)
    state_name = models.CharField(max_length=60, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.state_name} ({self.state_code}) - {self.gstin or 'No GST'}"


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

class Location(models.Model):
    """Geographic address attached to a user account."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="billing_locations",
    )
    location_type = models.CharField(max_length=20, choices=BookingType.choices)
    building_name = models.CharField(max_length=250)
    address_line1 = models.CharField(max_length=250)
    address_line2 = models.CharField(max_length=250, blank=True, null=True)
    locality = models.CharField(max_length=250)
    city = models.CharField(max_length=250)
    state = models.CharField(max_length=250)
    postal_code = models.CharField(max_length=20)

    class Meta:
        verbose_name = "Location"
        verbose_name_plural = "Locations"
        indexes = [
            models.Index(fields=["location_type"]),
            models.Index(fields=["city", "state"]),
        ]

    def __str__(self) -> str:
        return f"{self.building_name} ({self.city})"

    def full_address(self) -> str:
        parts = [
            self.building_name,
            self.address_line1,
            self.address_line2,
            self.locality,
            self.city,
            self.state,
            self.postal_code,
        ]
        return ", ".join(filter(None, parts))


# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------

class Package(models.Model):
    """Pricing package that can be attached to a Venue, Service, or Resource."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="billing_packages",
    )

    content_type = models.ForeignKey(
        ct_models.ContentType,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        limit_choices_to={"model__in": ["venue", "service", "resource"]},
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    belongs_to = ct_fields.GenericForeignKey("content_type", "object_id")

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    registration_fees = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    package_type = models.CharField(
        max_length=20,
        choices=BookingType.choices,
        default=BookingType.IN_HOUSE,
    )
    period = models.CharField(
        max_length=10,
        choices=PeriodChoices.choices,
        default=PeriodChoices.MONTHLY,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Package"
        verbose_name_plural = "Packages"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["package_type", "is_active"]),
            models.Index(fields=["owner", "-created_at"]),
        ]

    def save(self, *args, **kwargs):
        if self.content_type and self.content_type.model == "service" and self.belongs_to:
            if hasattr(self.belongs_to, "service_type"):
                self.package_type = self.belongs_to.service_type
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} ({self.package_type})"


# ---------------------------------------------------------------------------
# Patient
# ---------------------------------------------------------------------------

_PHONE_RE = RegexValidator(regex=r"^\d{10}$", message="Phone number must be 10 digits")


class Patient(models.Model):
    """Patient registration and medical profile."""

    GENDER_CHOICES = [
        ("male", "Male"),
        ("female", "Female"),
        ("other", "Other"),
        ("prefer-not-to-say", "Prefer not to say"),
    ]
    BLOOD_GROUP_CHOICES = [
        ("A+", "A+"), ("A-", "A-"),
        ("B+", "B+"), ("B-", "B-"),
        ("AB+", "AB+"), ("AB-", "AB-"),
        ("O+", "O+"), ("O-", "O-"),
    ]

    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="registered_patients",
        help_text="User who registered this patient",
    )

    patient_id = models.CharField(max_length=20, blank=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(null=True, blank=True)

    phone = models.CharField(max_length=10, validators=[_PHONE_RE])
    address = models.TextField()
    age = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(150)],
    )

    emergency_contact = models.CharField(max_length=100)
    emergency_phone = models.CharField(max_length=10, validators=[_PHONE_RE])
    emergency_contact_2 = models.CharField(max_length=100, null=True, blank=True)
    emergency_phone_2 = models.CharField(
        max_length=10, null=True, blank=True, validators=[_PHONE_RE]
    )

    medical_conditions = models.TextField(null=True, blank=True)
    allergies = models.TextField(null=True, blank=True)
    present_health_condition = models.TextField(null=True, blank=True)

    gender = models.CharField(max_length=20, choices=GENDER_CHOICES)
    blood_group = models.CharField(
        max_length=3, choices=BLOOD_GROUP_CHOICES, null=True, blank=True
    )
    preferred_language = models.CharField(max_length=20, null=True, blank=True)

    education_qualifications = models.TextField(null=True, blank=True)
    earlier_occupation = models.CharField(max_length=200, null=True, blank=True)
    year_of_retirement = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[
            MinValueValidator(1950),
            MaxValueValidator(timezone.localtime().year),
        ],
    )

    affiliate = models.CharField(max_length=100, null=True, blank=True)
    source = models.CharField(max_length=100, null=True, blank=True)
    referred_by = models.CharField(max_length=100, null=True, blank=True)
    is_probono = models.BooleanField(default=False)

    is_registration_fees_paid = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    registration_date = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Patient"
        verbose_name_plural = "Patients"
        ordering = ["-registration_date"]
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["phone"]),
            models.Index(fields=["-registration_date"]),
            models.Index(fields=["registered_by"]),
            models.Index(fields=["first_name", "last_name"]),
            models.Index(fields=["is_active", "is_deleted"]),
        ]

    def __str__(self) -> str:
        email_display = self.email or "No Email"
        return f"{self.get_full_name()} - {email_display}"

    def get_full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def soft_delete(self) -> None:
        self.is_active = False
        self.is_deleted = True
        self.save()

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and not self.patient_id:
            self.patient_id = f"{self.pk:05}"
            super().save(update_fields=["patient_id"])


# ---------------------------------------------------------------------------
# Patient Documents
# ---------------------------------------------------------------------------

class PatientDocument(models.Model):
    """Logical document group attached to a patient."""

    patient = models.ForeignKey(
        Patient, on_delete=models.CASCADE, related_name="documents"
    )
    title = models.CharField(max_length=255, blank=True, null=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    remarks = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.patient} - {self.title}"


class PatientDocumentFile(models.Model):
    """File attachment within a PatientDocument."""

    document = models.ForeignKey(
        PatientDocument, on_delete=models.CASCADE, related_name="files"
    )
    file = models.FileField(upload_to="patient_documents_report/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.document} - {self.file.name}"


# ---------------------------------------------------------------------------
# ContactBooking (staff-created booking)
# ---------------------------------------------------------------------------

class ContactBooking(models.Model):
    """Manually created booking record."""

    patient = models.ForeignKey(
        Patient,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manual_bookings",
        help_text="Linked patient account (optional for walk-ins)",
    )
    booked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_manual_bookings",
        help_text="Staff member who created this booking",
    )
    address = models.TextField()

    service = models.ForeignKey(
        "catalog.Service",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="manual_bookings",
    )

    start_date = models.DateField(help_text="Booking start date")
    end_date = models.DateField(help_text="Booking end date")
    description = models.TextField(
        blank=True,
        help_text="Additional notes or reason for booking",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date"]
        verbose_name = "Contact Booking"
        verbose_name_plural = "Contact Bookings"
        indexes = [
            models.Index(fields=["start_date", "end_date"]),
            models.Index(fields=["patient"]),
            models.Index(fields=["booked_by"]),
        ]

    def __str__(self) -> str:
        name = self.patient.get_full_name() if self.patient else "Walk-in"
        return f"#{self.pk} - {name}"


# ---------------------------------------------------------------------------
# PrimaryOrder
# ---------------------------------------------------------------------------

class PrimaryOrder(models.Model):
    """Top-level booking record spanning the full engagement period."""

    order_id = models.CharField(max_length=50, blank=True)
    booking_entity = models.CharField(
        max_length=20,
        choices=BookingEntity.choices,
        default=BookingEntity.VENUE,
        db_index=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        db_index=True,
        related_name="primary_orders",
    )
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, db_index=True)
    notes = models.TextField(blank=True, default="")

    start_datetime = models.DateTimeField(null=True, blank=True, db_index=True)
    end_datetime = models.DateTimeField(null=True, blank=True, db_index=True)
    raw_dates = models.JSONField(null=True, blank=True)

    total_bill = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"), editable=False
    )
    discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    premium_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    booking_type = models.CharField(
        max_length=25,
        choices=BookingType.choices,
        default=BookingType.OPD,
        db_index=True,
        blank=True,
    )
    status = models.CharField(
        max_length=25,
        choices=BookingStatus.choices,
        default=BookingStatus.LOBBY,
        db_index=True,
    )
    status_locked = models.BooleanField(default=False)
    auto_continue = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.order_id

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.order_id:
            self.order_id = generate_order_id(self)
            super().save(update_fields=["order_id"])
        self.recalculate_total()

    def _periods_from_dates(self, dates, package=None):
        pkg = package or (
            self.secondary_orders.order_by("-end_datetime").first().package
            if self.secondary_orders.exists()
            else None
        )
        if not pkg:
            return []
        period_type = pkg.period
        adjust = self.premium_amount - self.discount_amount

        if period_type == PeriodChoices.DAILY and isinstance(dates, list):
            bounds = [
                (datetime.combine(d, time.min), datetime.combine(d, time.max))
                for d in dates
            ]
        elif period_type == PeriodChoices.HOURLY and isinstance(dates, dict):
            bounds = [
                (datetime.combine(d, min(slots)), datetime.combine(d, max(slots)))
                for d, slots in dates.items()
                if slots
            ]
        else:
            return []

        periods = []
        for start, end in bounds:
            start, end = timezone.make_aware(start), timezone.make_aware(end)
            periods.append(
                (start, end, calculate_amount(start, end, pkg) + adjust)
            )
        return periods

    def _periods_full_range(self, package=None):
        pkg = package or (
            self.secondary_orders.order_by("-end_datetime").first().package
            if self.secondary_orders.exists()
            else None
        )
        if not pkg:
            return []
        generators = {
            PeriodChoices.MONTHLY: self._get_monthly_periods,
            PeriodChoices.WEEKLY: self._get_weekly_periods,
            PeriodChoices.DAILY: self._get_daily_periods,
            PeriodChoices.HOURLY: self._get_daily_periods,
        }
        generator = generators.get(pkg.period)
        if not generator:
            return []
        amount = pkg.price + self.premium_amount - self.discount_amount
        return [(s, e, amount) for s, e in generator()]

    def _get_monthly_periods(self):
        if not self.start_datetime or not self.end_datetime:
            return []
        periods = []
        current = self.start_datetime
        while current < self.end_datetime:
            next_period_start = current + relativedelta(months=1)
            period_end = min(next_period_start - timedelta(days=1), self.end_datetime)
            periods.append((current, period_end))
            current = next_period_start
        return periods

    def _get_weekly_periods(self):
        return self._split_into_days(chunk_days=7)

    def _get_daily_periods(self):
        return self._split_into_days()

    def _split_into_days(self, chunk_days=None):
        if not self.start_datetime or not self.end_datetime or self.start_datetime >= self.end_datetime:
            return []
        periods, current = [], self.start_datetime
        while current < self.end_datetime:
            chunk_end = (
                min(current + timedelta(days=chunk_days), self.end_datetime)
                if chunk_days
                else self.end_datetime
            )
            day_cursor = current
            while day_cursor < chunk_end:
                day_end = min(
                    day_cursor.replace(hour=23, minute=59, second=59, microsecond=999999),
                    self.end_datetime,
                )
                periods.append((day_cursor, day_end))
                day_cursor += timedelta(days=1)
            current = chunk_end
        return periods

    def _sync_secondary_orders(
        self,
        periods,
        *,
        package=None,
        service=None,
        venue=None,
        client_address=None,
        booking_type=None,
        status=None,
        upcoming_only=False,
        prune=False,
    ):
        MAX_PERIOD = timedelta(days=30)
        now = timezone.now()
        periods = [(s, min(e, s + MAX_PERIOD), a) for s, e, a in periods]
        if upcoming_only:
            periods = [p for p in periods if p[1] >= now]
        if not periods:
            return

        with transaction.atomic():
            existing = {
                (s.start_datetime, s.end_datetime): s
                for s in self.secondary_orders.filter(is_registration_fee=False).annotate(
                    ternary_total=Coalesce(Sum("ternary_orders__subtotal"), Decimal("0.00")),
                    ternary_count=Count("ternary_orders"),
                )
            }

            to_create, to_update = [], []
            matched, unmatched_new = set(), []

            def apply(row, start, end, amount):
                row.start_datetime = start
                row.end_datetime = end
                row.subtotal = amount + row.ternary_total
                if not row.status_locked:
                    row.status = status or auto_update_status(start, end)
                to_update.append(row)

            for start, end, amount in periods:
                current = existing.get((start, end))
                if current is None:
                    unmatched_new.append((start, end, amount))
                else:
                    matched.add(current.pk)
                    apply(current, start, end, amount)

            orphans = sorted(
                (s for s in existing.values() if s.pk not in matched),
                key=lambda s: s.start_datetime,
            )
            if prune and not upcoming_only:
                still_new = []
                for start, end, amount in unmatched_new:
                    if orphans:
                        apply(orphans.pop(0), start, end, amount)
                    else:
                        still_new.append((start, end, amount))
                unmatched_new = still_new

                stale = [s for s in orphans if s.end_datetime >= now]
                if blocked := [s for s in stale if s.ternary_count]:
                    dates = ", ".join(
                        s.start_datetime.date().isoformat() for s in blocked
                    )
                    raise ValidationError(
                        f"Move or cancel the services on these periods first: {dates}."
                    )
                SecondaryOrder.objects.filter(pk__in=[s.pk for s in stale]).delete()
                orphans = [s for s in orphans if s.end_datetime < now]

                taken = {(st, en) for st, en, _ in periods} | set(existing)
                for s in orphans:
                    new_end = s.start_datetime + MAX_PERIOD
                    if (
                        s.end_datetime - s.start_datetime > MAX_PERIOD
                        and (s.start_datetime, new_end) not in taken
                    ):
                        s.end_datetime = new_end
                        to_update.append(s)

            for start, end, amount in unmatched_new:
                to_create.append(
                    SecondaryOrder(
                        primary_order=self,
                        package=package,
                        service=service,
                        venue=venue,
                        client_address=client_address,
                        booking_type=booking_type or (package.package_type if package else BookingType.OPD),
                        start_datetime=start,
                        end_datetime=end,
                        subtotal=amount,
                        status=status or auto_update_status(start, end),
                    )
                )

            if to_update:
                SecondaryOrder.objects.bulk_update(
                    to_update, ["start_datetime", "end_datetime", "subtotal", "status"]
                )
            if to_create:
                created = SecondaryOrder.objects.bulk_create(to_create)
                for obj in created:
                    obj.order_id = generate_order_id(obj)
                SecondaryOrder.objects.bulk_update(created, ["order_id"])

            self.recalculate_total()

    def generate_secondary_from_random_dates(
        self,
        dates,
        package=None,
        service=None,
        venue=None,
        client_address=None,
        booking_type=None,
        upcoming_only=False,
        secondary_status=None,
        prune=False,
    ):
        if not dates:
            return
        self._sync_secondary_orders(
            self._periods_from_dates(dates, package=package),
            package=package,
            service=service,
            venue=venue,
            client_address=client_address,
            booking_type=booking_type,
            status=secondary_status,
            upcoming_only=upcoming_only,
            prune=prune,
        )

    def generate_secondary_full_range_dates(
        self,
        package=None,
        service=None,
        venue=None,
        client_address=None,
        booking_type=None,
        upcoming_only=False,
        secondary_status=None,
        prune=False,
    ):
        self._sync_secondary_orders(
            self._periods_full_range(package=package),
            package=package,
            service=service,
            venue=venue,
            client_address=client_address,
            booking_type=booking_type,
            status=secondary_status,
            upcoming_only=upcoming_only,
            prune=prune,
        )

    def generate_next_period_secondary(self):
        if not self.auto_continue:
            return None
        latest_sec = self.secondary_orders.order_by("-end_datetime").first()
        pkg = latest_sec.package if latest_sec else None
        if not pkg:
            return None
        period_type = pkg.period
        if period_type == PeriodChoices.MONTHLY:
            new_end = self.end_datetime + relativedelta(months=1)
        elif period_type == PeriodChoices.WEEKLY:
            new_end = self.end_datetime + timedelta(days=7)
        else:
            new_end = self.end_datetime + timedelta(days=1)
        with transaction.atomic():
            self.end_datetime = new_end
            self.save(update_fields=["end_datetime"])
            self.generate_secondary_full_range_dates(
                package=pkg,
                service=latest_sec.service if latest_sec else None,
                venue=latest_sec.venue if latest_sec else None,
                client_address=latest_sec.client_address if latest_sec else None,
                booking_type=latest_sec.booking_type if latest_sec else None,
                upcoming_only=True,
            )

    def _derive_status_from_secondaries(self) -> str:
        S = BookingStatus
        statuses = list(
            self.secondary_orders.filter(is_registration_fee=False)
            .order_by("start_datetime")
            .values_list("status", flat=True)
        )
        if not statuses:
            return self.status
        active = [s for s in statuses if s != S.CANCELLED]
        if not active:
            return S.CANCELLED
        if all(s == S.FULFILLED for s in active):
            return S.IN_PROGRESS if self.auto_continue else S.FULFILLED
        if any(s in (S.IN_PROGRESS, S.FULFILLED) for s in active):
            return S.IN_PROGRESS
        return active[0]

    def refresh_from_secondaries(self, sync_dates: bool = False) -> None:
        self.recalculate_total()
        updates: dict = {}
        if sync_dates:
            agg = self.secondary_orders.filter(is_registration_fee=False).aggregate(
                first=Min("start_datetime"), last=Max("end_datetime")
            )
            if agg["first"] and agg["last"]:
                updates["start_datetime"] = agg["first"]
                updates["end_datetime"] = agg["last"]
        if not self.status_locked:
            new_status = self._derive_status_from_secondaries()
            if new_status != self.status:
                updates["status"] = new_status
        if updates:
            updates["updated_at"] = timezone.now()
            PrimaryOrder.objects.filter(pk=self.pk).update(**updates)
            for field, value in updates.items():
                setattr(self, field, value)

    def recalculate_total(self) -> None:
        total = self.secondary_orders.aggregate(
            total=Coalesce(Sum("subtotal"), Decimal("0.00"))
        )["total"]
        self.total_bill = total
        super().save(update_fields=["total_bill"])


# ---------------------------------------------------------------------------
# SecondaryOrder
# ---------------------------------------------------------------------------

class SecondaryOrder(models.Model):
    """One record per billing period (month/week/day) within a PrimaryOrder span."""

    primary_order = models.ForeignKey(
        PrimaryOrder,
        on_delete=models.CASCADE,
        related_name="secondary_orders",
        null=True,
        blank=True,
        db_index=True,
    )
    order_id = models.CharField(max_length=50, blank=True)
    venue = models.ForeignKey(
        "catalog.Venue",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_index=True,
    )
    service = models.ForeignKey(
        "catalog.Service",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_index=True,
    )
    package = models.ForeignKey(
        Package,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="secondary_orders",
        db_index=True,
    )
    client_address = models.JSONField(null=True, blank=True)

    booking_entity = models.CharField(
        max_length=20,
        choices=BookingEntity.choices,
        default=BookingEntity.VENUE,
        db_index=True,
    )
    booking_type = models.CharField(
        max_length=25,
        choices=BookingType.choices,
        default=BookingType.OPD,
        db_index=True,
    )
    discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    premium_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    auto_continue = models.BooleanField(default=False)

    start_datetime = models.DateTimeField(db_index=True)
    end_datetime = models.DateTimeField(db_index=True)
    subtotal = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"), editable=False
    )
    status = models.CharField(
        max_length=25,
        choices=BookingStatus.choices,
        default=BookingStatus.LOBBY,
        db_index=True,
    )
    is_registration_fee = models.BooleanField(default=False, db_index=True)
    status_locked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("primary_order", "start_datetime", "end_datetime")
        ordering = ["start_datetime", "end_datetime"]

    def __str__(self) -> str:
        po_str = self.primary_order.order_id if self.primary_order else "NO_PO"
        return f"{po_str} → {self.order_id}"

    @property
    def booking_service(self) -> str | None:
        if self.is_registration_fee:
            return "Registration Fees"
        if self.service:
            return self.service.name
        if self.primary_order and hasattr(self.primary_order, "service") and self.primary_order.service:
            return self.primary_order.service.name
        return None

    @property
    def booking_package(self) -> str | None:
        if self.is_registration_fee:
            return "Registration Fees"
        if self.package:
            return self.package.name
        if self.primary_order and hasattr(self.primary_order, "package") and self.primary_order.package:
            return self.primary_order.package.name
        return None

    @property
    def booking_venue(self) -> str | None:
        if self.venue:
            return getattr(self.venue, "name", str(self.venue))
        if self.primary_order and hasattr(self.primary_order, "venue") and self.primary_order.venue:
            return getattr(self.primary_order.venue, "name", str(self.primary_order.venue))
        return None

    def save(self, *args, **kwargs):
        if self.package and not self.booking_type:
            self.booking_type = self.package.package_type
        skip_auto_status = kwargs.pop("skip_auto_status", False)
        kwargs.pop("skip_primary_recalc", None)
        is_targeted_save = kwargs.get("update_fields") is not None
        if not is_targeted_save and not skip_auto_status and not self.status_locked:
            self.status = auto_update_status(self.start_datetime, self.end_datetime)
        super().save(*args, **kwargs)
        if not self.order_id:
            self.order_id = generate_order_id(self)
            super().save(update_fields=["order_id"])

    def compute_base_amount(self) -> Decimal:
        po = self.primary_order
        discount = self.discount_amount if self.discount_amount else (po.discount_amount if po else Decimal("0.00"))
        premium = self.premium_amount if self.premium_amount else (po.premium_amount if po else Decimal("0.00"))
        adjust = premium - discount

        pkg = self.package or (getattr(po, "package", None) if po else None)
        if not pkg:
            return Decimal("0.00") + adjust

        raw_dates = po.raw_dates if po else None
        if raw_dates:
            base = calculate_amount(self.start_datetime, self.end_datetime, pkg)
        else:
            base = pkg.price
        return base + adjust

    def recalculate_subtotal(self) -> None:
        ternary_total = self.ternary_orders.aggregate(
            total=Coalesce(Sum("subtotal"), Decimal("0.00"))
        )["total"]
        self.subtotal = self.compute_base_amount() + ternary_total
        super().save(update_fields=["subtotal"])
        if self.primary_order_id:
            self.primary_order.refresh_from_secondaries()


# ---------------------------------------------------------------------------
# TernaryOrder
# ---------------------------------------------------------------------------

class TernaryOrder(models.Model):
    """One add-on service / line-item within a SecondaryOrder."""

    secondary_order = models.ForeignKey(
        SecondaryOrder,
        on_delete=models.CASCADE,
        related_name="ternary_orders",
        db_index=True,
    )
    order_id = models.CharField(max_length=50, blank=True)

    venue = models.ForeignKey(
        "catalog.Venue",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        db_index=True,
    )
    service = models.ForeignKey(
        "catalog.Service",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        db_index=True,
    )
    package = models.ForeignKey(
        Package,
        on_delete=models.CASCADE,
        related_name="ternary_orders",
    )

    booking_entity = models.CharField(
        max_length=20,
        choices=BookingEntity.choices,
        default=BookingEntity.SERVICE,
        db_index=True,
    )
    booking_type = models.CharField(
        max_length=25,
        choices=BookingType.choices,
        default=BookingType.OPD,
        db_index=True,
    )
    client_address = models.TextField(null=True, blank=True)

    start_datetime = models.DateTimeField(db_index=True)
    end_datetime = models.DateTimeField(db_index=True)

    discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    premium_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    subtotal = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"), editable=False
    )
    status = models.CharField(
        max_length=25,
        choices=BookingStatus.choices,
        default=BookingStatus.LOBBY,
        db_index=True,
    )
    status_locked = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_datetime"]

    def __str__(self) -> str:
        so_str = str(self.secondary_order) if self.secondary_order else "NO_SO"
        return f"{so_str} → {self.order_id}"

    @property
    def booking_service(self) -> str | None:
        return self.service.name if self.service else None

    @property
    def booking_package(self) -> str | None:
        return self.package.name if self.package else None

    def save(self, *args, **kwargs):
        skip_auto_status = kwargs.pop("skip_auto_status", False)
        update_fields = kwargs.get("update_fields")
        is_targeted_save = update_fields is not None

        if not is_targeted_save:
            if not skip_auto_status:
                self.status = auto_update_status(self.start_datetime, self.end_datetime)
            self.booking_type = self.package.package_type

        discount = self.discount_amount or Decimal("0.00")
        premium = self.premium_amount or Decimal("0.00")
        base_amount = calculate_amount(self.start_datetime, self.end_datetime, self.package)
        self.subtotal = base_amount + premium - discount

        super().save(*args, **kwargs)
        if not self.order_id:
            self.order_id = generate_order_id(self)
            super().save(update_fields=["order_id"])


# ---------------------------------------------------------------------------
# Quotation Models (Quote, QuoteLine, QuoteEvent)
# ---------------------------------------------------------------------------

class Quote(models.Model):
    """
    Independent Quotation model in the tenant workspace.

    Can target:
      • Venue, Service, or Resource
      • Patient or external client / business
    Can be converted directly into an Invoice.
    """

    number = models.CharField(max_length=30, db_index=True)
    revision = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(
        max_length=12,
        choices=QuoteStatus.choices,
        default=QuoteStatus.DRAFT,
        db_index=True,
    )

    # Subject / Target entity (Venue / Service / Resource)
    booking_entity = models.CharField(
        max_length=20,
        choices=BookingEntity.choices,
        null=True,
        blank=True,
        db_index=True,
    )
    venue = models.ForeignKey(
        "catalog.Venue",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quotes",
    )
    service = models.ForeignKey(
        "catalog.Service",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quotes",
    )
    resource = models.ForeignKey(
        "catalog.Resource",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quotes",
    )

    content_type = models.ForeignKey(
        ct_models.ContentType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = ct_fields.GenericForeignKey("content_type", "object_id")

    patient = models.ForeignKey(
        Patient,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quotes",
    )

    # Buyer / Customer Info
    company = models.CharField(max_length=200)
    attention = models.CharField(max_length=200, blank=True)
    address = models.TextField(blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    state = models.CharField(max_length=60, default="Karnataka")
    state_code = models.CharField(max_length=2, blank=True)
    buyer_email = models.EmailField()
    cc_emails = models.JSONField(default=list, blank=True)
    bcc_emails = models.JSONField(default=list, blank=True)
    ship_to = models.TextField(blank=True)
    your_ref = models.CharField(max_length=100, blank=True)

    # Dates & Terms
    quote_date = models.DateField(default=timezone.localdate)
    valid_days = models.PositiveSmallIntegerField(
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(365)],
    )
    notes = models.TextField(blank=True)
    terms = models.TextField(blank=True)

    # GST snapshot
    supply_type = models.CharField(
        max_length=12,
        choices=QuoteSupplyType.choices,
        default=QuoteSupplyType.UNKNOWN,
    )
    place_of_supply_code = models.CharField(max_length=2, blank=True)
    seller_gstin = models.CharField(max_length=15, blank=True)
    seller_state_code = models.CharField(max_length=2, blank=True)
    seller_state_name = models.CharField(max_length=60, blank=True)

    # Amounts
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    freight = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    packing = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    charges_gst_rate = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=18,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cgst = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sgst = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    igst = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    round_off = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Security & Audit
    accept_token = models.CharField(max_length=64, unique=True, default=token_urlsafe)
    sent_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="billing_created_quotes",
    )
    created_by_email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-quote_date", "-number", "-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["number", "revision"],
                name="billing_uniq_quote_number_revision",
            )
        ]
        indexes = [
            models.Index(fields=["status", "-quote_date"]),
            models.Index(fields=["company"]),
            models.Index(fields=["booking_entity"]),
        ]

    def __str__(self) -> str:
        return f"Quote {self.number} R{self.revision} ({self.status})"

    @property
    def valid_until(self):
        return self.quote_date + timedelta(days=self.valid_days)

    @property
    def state_tax_label(self) -> str:
        return "UTGST" if self.supply_type == QuoteSupplyType.INTRA_UT else "SGST"

    def send_on_mail(self, user=None, *, test_recipients=None, response_url=None):
        """Send this quotation by email."""
        from .services import send_quote
        return send_quote(self, user, test_recipients=test_recipients, response_url=response_url)

    def create_invoice(self, *, issue_now: bool = False) -> Invoice:
        """Convert this quote into a formal Invoice."""
        from .services import create_invoice_from_quote
        return create_invoice_from_quote(self, issue_now=issue_now)


class QuoteLine(models.Model):
    """Line item on a Quotation."""

    quote = models.ForeignKey(Quote, related_name="lines", on_delete=models.CASCADE)
    position = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=20, choices=QuoteLineKind.choices, default=QuoteLineKind.CUSTOM)
    item = models.CharField(max_length=200)
    specification = models.CharField(max_length=300, blank=True)
    hsn_sac = models.CharField(max_length=8, blank=True)
    uom = models.CharField(max_length=10, choices=Uom.choices, default=Uom.PCS)
    qty = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=1,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    lead_time = models.CharField(max_length=50, blank=True)
    gst_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=18,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    taxable_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["quote", "position"],
                name="billing_uniq_quote_line_position",
            )
        ]

    def __str__(self) -> str:
        return f"{self.quote.number} #{self.position}: {self.item}"


class QuoteEvent(models.Model):
    """Audit log for Quotations."""

    quote = models.ForeignKey(Quote, related_name="events", on_delete=models.CASCADE)
    kind = models.CharField(max_length=20)
    detail = models.CharField(max_length=300, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="billing_quote_events",
    )
    at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.quote.number} - {self.kind} @ {self.at}"


# ---------------------------------------------------------------------------
# Merged Unified Invoice Model
# ---------------------------------------------------------------------------

class Invoice(models.Model):
    """
    Unified Invoice Model for OHM Tenants.

    Can be:
      1. An independent invoice or quotation invoice
      2. Created specifically for a Venue, Service, or Resource
      3. Generated from a Quote
      4. Linked to Patient booking orders (SecondaryOrder / TernaryOrder)
      5. Carries itemized GST lines (InvoiceItem)
      6. Supports sending via email with attachments (`send_on_mail`)
    """

    # Identity
    invoice_number = models.CharField(max_length=50, unique=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=InvoiceStatus.choices,
        default=InvoiceStatus.DRAFT,
        db_index=True,
    )
    invoice_type_tag = models.CharField(
        max_length=30,
        choices=InvoiceType.choices,
        default=InvoiceType.STANDARD,
        db_index=True,
    )

    # Subject / Target entity (Venue / Service / Resource)
    booking_entity = models.CharField(
        max_length=20,
        choices=BookingEntity.choices,
        null=True,
        blank=True,
        db_index=True,
    )
    venue = models.ForeignKey(
        "catalog.Venue",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="billing_invoices",
    )
    service = models.ForeignKey(
        "catalog.Service",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="billing_invoices",
    )
    resource = models.ForeignKey(
        "catalog.Resource",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="billing_invoices",
    )

    content_type = models.ForeignKey(
        ct_models.ContentType,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = ct_fields.GenericForeignKey("content_type", "object_id")

    # Links to Quotation & Booking orders
    quote = models.ForeignKey(
        Quote,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoices",
    )
    secondary_order = models.ForeignKey(
        SecondaryOrder,
        null=True,
        blank=True,
        related_name="invoices",
        on_delete=models.SET_NULL,
    )
    ternary_order = models.ForeignKey(
        TernaryOrder,
        null=True,
        blank=True,
        related_name="invoices",
        on_delete=models.SET_NULL,
    )
    parent_invoice = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        related_name="child_invoices",
        on_delete=models.SET_NULL,
    )

    # Customer / Patient / Account
    patient = models.ForeignKey(
        Patient,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoices",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="billing_invoices",
    )

    customer_name = models.CharField(max_length=200, blank=True)
    attention = models.CharField(max_length=200, blank=True)
    address = models.TextField(blank=True)
    buyer_email = models.EmailField(blank=True)
    cc_emails = models.JSONField(default=list, blank=True)
    bcc_emails = models.JSONField(default=list, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    state = models.CharField(max_length=60, default="Karnataka", blank=True)
    state_code = models.CharField(max_length=2, blank=True)
    is_interstate = models.BooleanField(default=False)
    ship_to = models.TextField(blank=True)
    your_ref = models.CharField(max_length=100, blank=True)

    # Issuer snapshot (company header + contact)
    issuer = models.JSONField(default=dict, blank=True)

    # Dates
    issue_date = models.DateField(default=timezone.localdate)
    due_days = models.PositiveSmallIntegerField(default=15)
    due_date = models.DateField(null=True, blank=True)
    period_start = models.DateTimeField(null=True, blank=True, db_index=True)
    period_end = models.DateTimeField(null=True, blank=True, db_index=True)

    # Amounts
    currency = models.CharField(max_length=3, default="INR")
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    premium_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    round_off = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    amount_received = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    remaining_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Text, PDF & Metadata
    terms = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    pdf = models.FileField(upload_to="invoices/", null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-issue_date", "-created_at"]
        indexes = [
            models.Index(fields=["status", "due_date"]),
            models.Index(fields=["booking_entity"]),
            models.Index(fields=["patient", "status"]),
        ]

    def __str__(self) -> str:
        num = self.invoice_number or f"DRAFT-#{self.id}"
        return f"{num} | {self.customer_name or 'Invoice'} ({self.status})"

    @property
    def invoice_type(self) -> str:
        if self.quote_id:
            return "QUOTATION"
        if self.secondary_order_id:
            return "SECONDARY"
        if self.ternary_order_id:
            return "TERNARY"
        if self.booking_entity == BookingEntity.VENUE or self.venue_id:
            return "VENUE"
        if self.booking_entity == BookingEntity.SERVICE or self.service_id:
            return "SERVICE"
        if self.booking_entity == BookingEntity.RESOURCE or self.resource_id:
            return "RESOURCE"
        return self.invoice_type_tag or "STANDARD"

    @property
    def balance_due(self) -> Decimal:
        return max(self.total_amount - (self.paid_amount or Decimal("0.00")), Decimal("0.00"))

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def save(self, *args, **kwargs):
        # Auto-fill customer details from patient if empty
        if self.patient and not self.customer_name:
            self.customer_name = self.patient.get_full_name()
            if not self.buyer_email and self.patient.email:
                self.buyer_email = self.patient.email
            if not self.address and self.patient.address:
                self.address = self.patient.address

        if not self.due_date and self.issue_date:
            self.due_date = self.issue_date + timedelta(days=self.due_days)

        is_new = self.pk is None
        super().save(*args, **kwargs)

        if is_new and not self.invoice_number:
            prefix = "INV"
            if self.secondary_order_id:
                prefix = "SINV"
            elif self.ternary_order_id:
                prefix = "TINV"
            elif self.quote_id:
                prefix = "QINV"
            elif self.venue_id:
                prefix = "VINV"
            elif self.service_id:
                prefix = "SRVINV"
            elif self.resource_id:
                prefix = "RESINV"

            self.invoice_number = f"{prefix}{self.id:08}"
            super().save(update_fields=["invoice_number"])

    def refresh_payment_status(self):
        """Update invoice status based on amount paid vs total."""
        if self.status in {InvoiceStatus.VOID, InvoiceStatus.CANCELLED}:
            return
        paid = self.paid_amount
        if self.total_amount > 0 and paid >= self.total_amount:
            self.status = InvoiceStatus.PAID
        elif paid > 0:
            self.status = InvoiceStatus.PARTIALLY_PAID
        elif self.due_date and self.due_date < timezone.localdate():
            self.status = InvoiceStatus.OVERDUE
        elif self.status == InvoiceStatus.DRAFT:
            pass
        else:
            self.status = InvoiceStatus.ISSUED
        self.save(update_fields=["status", "updated_at"])

    def recalculate_payments(self) -> None:
        """Recompute paid_amount, remaining_amount and status from payments."""
        self.paid_amount = (
            self.payments.filter(
                is_verified=True
            ).aggregate(total=Coalesce(Sum("amount"), Decimal("0.00")))["total"]
            + (self.amount_received or Decimal("0.00"))
        )
        self.remaining_amount = self.balance_due
        self.refresh_payment_status()
        super().save(update_fields=["paid_amount", "remaining_amount", "status"])

    def recalculate_from_items(self) -> None:
        """Recalculate invoice totals based on its line items."""
        items = list(self.items.all())
        if items:
            self.subtotal = sum((i.taxable_value for i in items), Decimal("0.00"))
            self.discount_amount = sum((i.discount_amount for i in items), Decimal("0.00"))
            self.cgst_amount = sum((i.cgst_amount for i in items), Decimal("0.00"))
            self.sgst_amount = sum((i.sgst_amount for i in items), Decimal("0.00"))
            self.igst_amount = sum((i.igst_amount for i in items), Decimal("0.00"))
            self.tax_amount = self.cgst_amount + self.sgst_amount + self.igst_amount
            self.total_amount = sum((i.amount for i in items), Decimal("0.00"))
            self.remaining_amount = self.balance_due
            super().save(
                update_fields=[
                    "subtotal", "discount_amount", "cgst_amount",
                    "sgst_amount", "igst_amount", "tax_amount",
                    "total_amount", "remaining_amount"
                ]
            )

    # ── Mail Sending ───────────────────────────────────────────────────────

    def send_on_mail(self, user=None, *, test_recipients=None) -> Invoice:
        """
        Send this invoice via email to the customer / buyer.
        Attaches HTML template and optional PDF file.
        """
        from .services import send_invoice_email
        return send_invoice_email(self, user=user, test_recipients=test_recipients)

    # ── Factories for Bookings ─────────────────────────────────────────────

    @classmethod
    def create_or_update_for_secondary(cls, secondary: SecondaryOrder) -> Invoice:
        primary = secondary.primary_order
        with transaction.atomic():
            invoice, created = cls.objects.get_or_create(
                secondary_order=secondary,
                defaults={
                    "period_start": secondary.start_datetime,
                    "period_end": secondary.end_datetime,
                    "patient": primary.patient,
                    "user": primary.user,
                    "customer_name": primary.patient.get_full_name(),
                    "buyer_email": primary.patient.email or "",
                    "address": primary.patient.address or "",
                    "subtotal": secondary.subtotal,
                    "total_amount": secondary.subtotal,
                    "remaining_amount": secondary.subtotal,
                    "issue_date": (secondary.end_datetime.date() + timedelta(days=1)) if secondary.end_datetime else timezone.localdate(),
                    "status": InvoiceStatus.ISSUED,
                    "invoice_type_tag": InvoiceType.BOOKING_SECONDARY,
                    "booking_entity": getattr(secondary, "booking_entity", getattr(primary, "booking_entity", BookingEntity.VENUE)),
                    "venue": secondary.venue,
                    "service": secondary.service,
                },
            )
            if not created:
                invoice.subtotal = secondary.subtotal
                invoice.total_amount = (
                    invoice.subtotal
                    + invoice.premium_amount
                    + invoice.tax_amount
                    - invoice.discount_amount
                )
                invoice.remaining_amount = invoice.balance_due
                super(Invoice, invoice).save(
                    update_fields=["subtotal", "total_amount", "remaining_amount"]
                )
        return invoice

    @classmethod
    def create_or_update_for_ternary(cls, ternary: TernaryOrder) -> Invoice:
        secondary = ternary.secondary_order
        primary = secondary.primary_order
        parent_invoice = secondary.invoices.filter(secondary_order=secondary).first()

        with transaction.atomic():
            invoice, created = cls.objects.get_or_create(
                ternary_order=ternary,
                defaults={
                    "period_start": ternary.start_datetime,
                    "period_end": ternary.end_datetime,
                    "parent_invoice": parent_invoice,
                    "patient": primary.patient,
                    "user": primary.user,
                    "customer_name": primary.patient.get_full_name(),
                    "buyer_email": primary.patient.email or "",
                    "address": primary.patient.address or "",
                    "subtotal": ternary.subtotal,
                    "total_amount": ternary.subtotal,
                    "remaining_amount": ternary.subtotal,
                    "issue_date": (ternary.end_datetime.date() + timedelta(days=1)) if ternary.end_datetime else timezone.localdate(),
                    "status": InvoiceStatus.ISSUED,
                    "invoice_type_tag": InvoiceType.BOOKING_TERNARY,
                    "booking_entity": ternary.booking_entity,
                    "venue": ternary.venue,
                    "service": ternary.service,
                },
            )
            if not created:
                invoice.subtotal = ternary.subtotal
                invoice.total_amount = (
                    invoice.subtotal
                    + invoice.premium_amount
                    + invoice.tax_amount
                    - invoice.discount_amount
                )
                invoice.remaining_amount = invoice.balance_due
                if parent_invoice and not invoice.parent_invoice_id:
                    invoice.parent_invoice = parent_invoice
                super(Invoice, invoice).save(
                    update_fields=["subtotal", "total_amount", "remaining_amount", "parent_invoice"]
                )
        return invoice


# ---------------------------------------------------------------------------
# Invoice Line Item
# ---------------------------------------------------------------------------

class InvoiceItem(models.Model):
    """Detailed itemized line on an Invoice (GST, HSN/SAC, discounts)."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    position = models.PositiveSmallIntegerField(default=1)
    line_type = models.CharField(
        max_length=20,
        choices=InvoiceLineType.choices,
        default=InvoiceLineType.CUSTOM,
    )
    description = models.CharField(max_length=255)
    hsn_sac = models.CharField(max_length=8, blank=True)
    specification = models.CharField(max_length=255, blank=True)
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=1,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    uom = models.CharField(max_length=20, choices=Uom.choices, default=Uom.PCS)
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    lead_time = models.CharField(max_length=50, blank=True)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("18.00"))

    # Computed fields
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    taxable_value = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.invoice.invoice_number} #{self.position}: {self.description}"


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------

class Payment(models.Model):
    """Payment record linked to an Invoice."""

    invoice = models.ForeignKey(
        Invoice,
        related_name="payments",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    patient = models.ForeignKey(
        Patient,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="INR")
    method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    paid_date = models.DateTimeField(default=timezone.now)
    reference = models.CharField(max_length=100, blank=True)

    is_verified = models.BooleanField(default=False)
    mapping_status = models.CharField(
        max_length=20,
        choices=PaymentMappingStatus.choices,
        default=PaymentMappingStatus.UNMAPPED,
    )
    mapping_meta = models.JSONField(null=True, blank=True)

    gateway = models.CharField(max_length=30, blank=True)
    gateway_order_id = models.CharField(max_length=100, blank=True)
    gateway_payment_id = models.CharField(max_length=100, null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="recorded_billing_payments",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-paid_date"]
        indexes = [
            models.Index(fields=["invoice", "created_at"]),
            models.Index(fields=["is_verified"]),
        ]

    def __str__(self) -> str:
        invoice_num = self.invoice.invoice_number if self.invoice else "UNMAPPED"
        return f"Payment #{self.id} — {self.amount} → {invoice_num}"

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"PAY-{uuid.uuid4().hex[:10].upper()}"
        super().save(*args, **kwargs)

    def _set_verified(self, state: bool) -> bool:
        if self.is_verified == state:
            return False
        with transaction.atomic():
            self.is_verified = state
            super(Payment, self).save(update_fields=["is_verified"])
            if self.invoice:
                self.invoice.recalculate_payments()
        return True

    def verify(self) -> bool:
        return self._set_verified(True)

    def unverify(self) -> bool:
        return self._set_verified(False)


# ---------------------------------------------------------------------------
# Aliases for backwards compatibility
# ---------------------------------------------------------------------------
TotalInvoice = Invoice
QuotationInvoice = Invoice
Quotation = Quote
QuotationItem = QuoteLine
QuotationInvoiceItem = InvoiceItem
QuotationPayment = Payment
