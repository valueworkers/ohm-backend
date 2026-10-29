import secrets
import uuid
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q, Sum
from django_tenants.models import DomainMixin, TenantMixin
from django.utils import timezone

GSTIN_RE = r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"

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
    class BillingPeriod(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        YEARLY = "yearly", "Yearly"
        ONE_TIME = "one_time", "One time"
    code = models.SlugField(max_length=50, unique=True)  # e.g. basic, professional
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    features = models.ManyToManyField(Feature, blank=True, related_name="plans")
    max_employees = models.PositiveIntegerField(null=True, blank=True)  # null = unlimited
    max_branches = models.PositiveIntegerField(null=True, blank=True)   # null = unlimited
    billing_period = models.CharField(max_length=20, choices=BillingPeriod.choices, default=BillingPeriod.MONTHLY)
    currency = models.CharField(max_length=3, default="INR")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    setup_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("18.00"), validators=[MinValueValidator(0), MaxValueValidator(100)])
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
    # Users who own this tenant; all have equal access to manage the workspace.
    owners = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="owned_tenants"
    )

    # --- Organization profile (copied from the approved onboarding request) ---
    logo = models.ImageField(upload_to="tenants/logos/", null=True, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=30, blank=True)
    gst_number = models.CharField(
        max_length=15, null=True, blank=True,
        validators=[RegexValidator(GSTIN_RE, "Enter a valid 15-character GSTIN, e.g. 22AAAAA0000A1Z5.")],
    )
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    tags = models.JSONField(default=list, blank=True)  # e.g. ["hospital", "clinic", "lab"]
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
    price_billing_period = models.CharField(max_length=20, choices=Plan.BillingPeriod.choices, blank=True)
    price_currency = models.CharField(max_length=3, blank=True)
    price_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_setup_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_tax_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
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
    branches = models.JSONField(
        default=list,
        help_text='{"name": "","line1": "", "line2": "", "locality": "", "city": "", "pincode": ""}',
    )
    ops_admin_email = models.EmailField(blank=True,null=True)               # null=True = no ops admin
    slug = models.SlugField(max_length=30)                        # Subdomain -> slug.o-hm.com
    custom_domain = models.CharField(max_length=253, blank=True)  # blank = no own domain
    wants_custom_website = models.BooleanField(default=False)     # Customized website
    requested_plan = models.ForeignKey(
        Plan, null=True, blank=True, on_delete=models.SET_NULL, related_name="onboarding_requests"
    )
    gst_number = models.CharField(max_length=15,null=True, blank=True, validators=[
        RegexValidator(GSTIN_RE,"Enter a valid 15-character GSTIN, e.g. 22AAAAA0000A1Z5.")
    ])

    co_owner_emails = models.JSONField(
        default=list, blank=True, null=True
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
    welcome_email_sent_at = models.DateTimeField(null=True, blank=True)
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


class DocumentSequence(models.Model):
    """Transactional per-prefix/year counters for quotation and invoice numbers."""

    prefix = models.CharField(max_length=8)
    year = models.PositiveSmallIntegerField()
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["prefix", "year"], name="uniq_document_sequence_prefix_year")]


class Quotation(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"

    number = models.CharField(max_length=30, unique=True)
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.SET_NULL, related_name="quotations")
    provisioning_request = models.ForeignKey(
        TenantProvisioning, null=True, blank=True, on_delete=models.SET_NULL, related_name="quotations"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="quotations")
    billing_period = models.CharField(max_length=20, choices=Plan.BillingPeriod.choices, default=Plan.BillingPeriod.MONTHLY)
    plan_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    setup_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    billing_name = models.CharField(max_length=200)
    billing_email = models.EmailField()
    gst_number = models.CharField(max_length=15, null=True, blank=True)
    billing_address = models.JSONField(default=dict, blank=True)
    currency = models.CharField(max_length=3, default="INR")
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("18.00"),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    valid_until = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    terms = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="created_quotations",
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(discount__gte=0) & Q(tax_rate__gte=0) & Q(tax_rate__lte=100),
                name="quotation_discount_and_tax_nonnegative",
            ),
        ]

    def clean(self):
        has_tenant = bool(self.tenant_id)
        has_request = bool(self.provisioning_request_id)
        if (self._state.adding and has_tenant == has_request) or (has_tenant and has_request):
            raise ValidationError("A quotation must target exactly one tenant or provisioning request.")

    def __str__(self):
        return f"{self.number} ({self.status})"


class QuotationItem(models.Model):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gte=1), name="quotation_item_quantity_positive")]


class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"
        VOID = "void", "Void"

    number = models.CharField(max_length=30, unique=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="invoices")
    subscription = models.ForeignKey(
        Subscription, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    quotation = models.ForeignKey(
        Quotation, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    billing_name = models.CharField(max_length=200)
    billing_email = models.EmailField()
    gst_number = models.CharField(max_length=15, null=True, blank=True)
    billing_address = models.JSONField(default=dict, blank=True)
    currency = models.CharField(max_length=3, default="INR")
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("18.00"),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    pdf = models.FileField(upload_to="invoices/", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-issue_date", "-created_at"]
        indexes = [models.Index(fields=["tenant", "status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["quotation"], condition=Q(quotation__isnull=False),
                name="uniq_invoice_per_quotation",
            ),
            models.CheckConstraint(
                condition=Q(discount__gte=0) & Q(tax_rate__gte=0) & Q(tax_rate__lte=100),
                name="invoice_discount_and_tax_nonnegative",
            ),
        ]

    def __str__(self):
        return f"{self.number} ({self.status})"

    @property
    def amount_paid(self):
        return self.payments.filter(status=Payment.Status.SUCCEEDED).aggregate(total=Sum("amount"))["total"] or Decimal("0")

    @property
    def balance_due(self):
        return self.total - self.amount_paid

    def refresh_payment_status(self):
        """Recalculate status from successful payments, including refunds."""
        if self.status in {self.Status.VOID, self.Status.DRAFT}:
            return
        paid = self.amount_paid
        if self.total > 0 and paid >= self.total:
            self.status = self.Status.PAID
        elif paid > 0:
            self.status = self.Status.PARTIALLY_PAID
        elif self.due_date and self.due_date < timezone.localdate():
            self.status = self.Status.OVERDUE
        else:
            self.status = self.Status.ISSUED
        self.save(update_fields=["status", "updated_at"])


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gte=1), name="invoice_item_quantity_positive")]


class Payment(models.Model):
    class Method(models.TextChoices):
        CARD = "card", "Card"
        UPI = "upi", "UPI"
        NETBANKING = "netbanking", "Net banking"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        CASH = "cash", "Cash"
        CHEQUE = "cheque", "Cheque"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    currency = models.CharField(max_length=3, default="INR")
    method = models.CharField(max_length=20, choices=Method.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    gateway = models.CharField(max_length=30, blank=True)
    gateway_order_id = models.CharField(max_length=100, blank=True)
    gateway_payment_id = models.CharField(max_length=100, null=True, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    gateway_response = models.JSONField(default=dict, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="recorded_payments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["gateway", "gateway_payment_id"],
                condition=Q(gateway_payment_id__isnull=False),
                name="uniq_gateway_payment_id",
            ),
        ]

    def __str__(self):
        return f"{self.invoice.number} - {self.amount} ({self.status})"


class PlatformAdmin(models.Model):
    """Public-schema platform privileges linked to the shared auth user model."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="platform_admin_profile")
    title = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.user.email


class QuoteCounter(models.Model):
    """Monthly sequence for prospect quote numbers."""
    period = models.CharField(max_length=4, unique=True)
    last_seq = models.PositiveIntegerField(default=0)


class QuoteTaxSettings(models.Model):
    """Seller GST state configured once for the public control plane or per tenant."""
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.CASCADE, related_name="quote_tax_settings")
    state_code = models.CharField(max_length=2, blank=True)
    state_name = models.CharField(max_length=60, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant"], condition=Q(tenant__isnull=True), name="uniq_public_quote_tax_settings"),
            models.UniqueConstraint(fields=["tenant"], condition=Q(tenant__isnull=False), name="uniq_tenant_quote_tax_settings"),
        ]

    def __str__(self):
        scope = self.tenant.name if self.tenant_id else "Public"
        return f"{scope}: {self.state_name} ({self.state_code})"


class Quote(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"
        SUPERSEDED = "superseded", "Superseded"

    number = models.CharField(max_length=24, db_index=True)
    revision = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    company = models.CharField(max_length=200)
    attention = models.CharField(max_length=200, blank=True)
    address = models.TextField(blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    state = models.CharField(max_length=60)
    buyer_email = models.EmailField()
    ship_to = models.TextField(blank=True)
    your_ref = models.CharField(max_length=100, blank=True)
    quote_date = models.DateField(default=timezone.localdate)
    valid_days = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(1), MaxValueValidator(365)])
    notes = models.TextField(blank=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cgst = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sgst = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    igst = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    freight = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    accept_token = models.CharField(max_length=64, unique=True, default=secrets.token_urlsafe)
    sent_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    provisioning = models.ForeignKey(TenantProvisioning, null=True, blank=True, on_delete=models.SET_NULL, related_name="prospect_quotes")
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.CASCADE, related_name="prospect_quotes")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="prospect_quotes")
    created_by_email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-quote_date", "-number", "-revision"]
        constraints = [models.UniqueConstraint(fields=["number", "revision"], name="uniq_quote_number_revision")]

    @property
    def valid_until(self):
        from datetime import timedelta
        return self.quote_date + timedelta(days=self.valid_days)


class QuoteLine(models.Model):
    class Kind(models.TextChoices):
        CATALOG = "catalog", "Catalog"
        CUSTOM = "custom", "Custom"
        SERVICE_VISIT = "service_visit", "Service visit"
        ON_REQUEST = "on_request", "On request"

    quote = models.ForeignKey(Quote, related_name="lines", on_delete=models.CASCADE)
    position = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=14, choices=Kind.choices)
    plan = models.ForeignKey(Plan, null=True, blank=True, on_delete=models.SET_NULL, related_name="quote_lines")
    item = models.CharField(max_length=200)
    specification = models.CharField(max_length=300, blank=True)
    hsn_sac = models.CharField(max_length=8, blank=True)
    qty = models.DecimalField(max_digits=8, decimal_places=2, default=1, validators=[MinValueValidator(Decimal("0.01"))])
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    lead_time = models.CharField(max_length=50, blank=True)
    gst_rate = models.DecimalField(max_digits=4, decimal_places=2, default=18, validators=[MinValueValidator(0), MaxValueValidator(100)])

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["quote", "position"], name="uniq_quote_line_position")]


class QuoteEvent(models.Model):
    quote = models.ForeignKey(Quote, related_name="events", on_delete=models.CASCADE)
    kind = models.CharField(max_length=20)
    detail = models.CharField(max_length=300, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    at = models.DateTimeField(auto_now_add=True)
