from django.contrib import admin, messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone

from .models import (
    Domain,
    Feature,
    DocumentSequence,
    Invoice,
    InvoiceItem,
    Payment,
    Plan,
    PlatformAdmin,
    Quotation,
    QuotationItem,
    Subscription,
    Tenant,
    TenantProvisioning,
)
from .services import (
    BillingError,
    accept_quotation,
    create_invoice_from_accepted_quotation,
    issue_invoice,
    recalculate_billing_totals,
    send_quotation,
    _next_document_number,
)


class DomainInline(admin.TabularInline):
    model = Domain
    extra = 0
    fields = ("domain", "is_primary", "verified", "verified_at")
    readonly_fields = ("verified_at",)


class SubscriptionInline(admin.TabularInline):
    model = Subscription
    extra = 0
    fields = ("plan", "price_billing_period", "price_currency", "price_amount", "status", "starts_at", "ends_at", "trial_ends_at")
    show_change_link = True


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    prepopulated_fields = {"code": ("name",)}


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "billing_period", "currency", "amount", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    filter_horizontal = ("features",)
    prepopulated_fields = {"code": ("name",)}


class QuotationItemInline(admin.TabularInline):
    model = QuotationItem
    extra = 0

    def has_add_permission(self, request, obj=None):
        return obj is None or obj.status == Quotation.Status.DRAFT

    def has_change_permission(self, request, obj=None):
        return obj is None or obj.status == Quotation.Status.DRAFT

    def has_delete_permission(self, request, obj=None):
        return obj is None or obj.status == Quotation.Status.DRAFT


@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    list_display = ("number", "billing_name", "plan", "status", "total", "currency", "valid_until", "created_at")
    list_filter = ("status", "currency", "plan")
    search_fields = ("number", "billing_name", "billing_email")
    readonly_fields = (
        "number", "status", "subtotal", "tax_amount", "total", "sent_at",
        "responded_at", "created_at", "updated_at",
    )
    inlines = (QuotationItemInline,)
    actions = ("send_selected", "accept_selected", "create_invoices")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.number = _next_document_number("QT")
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != Quotation.Status.DRAFT:
            return tuple(field.name for field in self.model._meta.fields if field.name != "id")
        return self.readonly_fields

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        try:
            recalculate_billing_totals(form.instance)
        except BillingError as exc:
            raise DjangoValidationError(str(exc)) from exc

    @admin.action(description="Send selected draft quotations")
    def send_selected(self, request, queryset):
        self._run_quotation_action(request, queryset, send_quotation, "quotation(s) sent")

    @admin.action(description="Mark selected sent quotations as accepted")
    def accept_selected(self, request, queryset):
        self._run_quotation_action(request, queryset, accept_quotation, "quotation(s) accepted")

    @admin.action(description="Create invoices for selected accepted quotations")
    def create_invoices(self, request, queryset):
        created = 0
        errors = []
        for quote in queryset:
            try:
                create_invoice_from_accepted_quotation(quote.pk)
                created += 1
            except BillingError as exc:
                errors.append(f"{quote.number}: {exc}")
        self.message_user(request, f"Invoices ready: {created}.", messages.SUCCESS if not errors else messages.WARNING)
        for error in errors:
            self.message_user(request, error, messages.ERROR)

    def _run_quotation_action(self, request, queryset, action, success_text):
        changed = 0
        errors = []
        for quote in queryset:
            try:
                action(quote.pk)
                changed += 1
            except BillingError as exc:
                errors.append(f"{quote.number}: {exc}")
        self.message_user(request, f"{changed} {success_text}.", messages.SUCCESS if not errors else messages.WARNING)
        for error in errors:
            self.message_user(request, error, messages.ERROR)


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0
    readonly_fields = ("description", "quantity", "unit_price", "amount")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = (
        "id", "invoice", "amount", "currency", "method", "status", "gateway",
        "gateway_order_id", "gateway_payment_id", "reference", "gateway_response",
        "failure_reason", "paid_at", "recorded_by", "created_at",
    )
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "tenant", "status", "total", "currency", "issue_date", "due_date")
    list_filter = ("status", "currency", "issue_date")
    search_fields = ("number", "tenant__name", "billing_name", "billing_email")
    readonly_fields = (
        "number", "tenant", "subscription", "quotation", "status", "billing_name",
        "billing_email", "gst_number", "billing_address", "currency", "subtotal",
        "discount", "tax_rate", "tax_amount", "total", "period_start", "period_end",
        "issue_date", "due_date", "notes", "pdf", "created_at", "updated_at",
    )
    inlines = (InvoiceItemInline, PaymentInline)
    actions = ("issue_selected",)

    def has_add_permission(self, request):
        return False

    @admin.action(description="Issue selected draft invoices")
    def issue_selected(self, request, queryset):
        changed = 0
        errors = []
        for invoice in queryset:
            try:
                issue_invoice(invoice.pk)
                changed += 1
            except BillingError as exc:
                errors.append(f"{invoice.number}: {exc}")
        self.message_user(request, f"{changed} invoice(s) issued.", messages.SUCCESS if not errors else messages.WARNING)
        for error in errors:
            self.message_user(request, error, messages.ERROR)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("invoice", "gateway", "gateway_payment_id", "amount", "currency", "method", "status", "paid_at")
    list_filter = ("gateway", "method", "status", "currency")
    search_fields = ("invoice__number", "gateway_payment_id", "reference")
    readonly_fields = ("id", "created_at", "paid_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(admin.ModelAdmin):
    list_display = ("prefix", "year", "last_value")
    readonly_fields = ("prefix", "year", "last_value")


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "schema_name", "status", "contact_email", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "schema_name", "contact_email", "domains__domain")
    readonly_fields = ("schema_name", "created_at")
    filter_horizontal = ("owners",)
    inlines = [DomainInline, SubscriptionInline]

    def get_readonly_fields(self, request, obj=None):
        # schema_name is generated by the provisioning service; never edit it
        # on an existing tenant. Creating tenants should go through onboarding.
        if obj is None:
            return ("created_at",)
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        # Deleting a tenant row must be a deliberate, scripted operation.
        return False


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ("domain", "tenant", "is_primary", "verified", "verified_at")
    list_filter = ("verified", "is_primary")
    search_fields = ("domain", "tenant__name", "tenant__schema_name")
    readonly_fields = ("verification_token", "verified_at", "created_at")
    actions = ["mark_verified"]

    @admin.action(description="Mark selected domains as verified")
    def mark_verified(self, request, queryset):
        updated = queryset.filter(verified=False).update(verified=True, verified_at=timezone.now())
        self.message_user(request, f"{updated} domain(s) marked as verified.", messages.SUCCESS)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("tenant", "plan", "price_billing_period", "price_currency", "price_amount", "status", "starts_at", "ends_at", "trial_ends_at")
    list_filter = ("status", "plan")
    search_fields = ("tenant__name", "tenant__schema_name", "billing_email")
    autocomplete_fields = ("tenant",)
    filter_horizontal = ("features",)
    date_hierarchy = "starts_at"


@admin.register(TenantProvisioning)
class TenantProvisioningAdmin(admin.ModelAdmin):
    list_display = (
        "organization_name", "slug", "custom_domain", "status",
        "applicant", "requested_plan", "created_at",
    )
    list_filter = ("status", "wants_custom_website", "requested_plan")
    search_fields = ("organization_name", "slug", "custom_domain", "email", "applicant__email")
    date_hierarchy = "created_at"
    raw_id_fields = ("applicant", "reviewed_by", "tenant")
    readonly_fields = (
        "id", "applicant", "status", "reviewed_by", "reviewed_at", "tenant",
        "provision_error", "created_at", "updated_at",
    )
    fieldsets = (
        ("Organization", {"fields": ("organization_name", "logo", "email", "contact_phone", "gst_number", "branches")}),
        ("Hosting", {"fields": ("slug", "custom_domain", "wants_custom_website", "ops_admin_email", "co_owner_emails", "requested_plan")}),
        ("Review", {"fields": ("id", "applicant", "status", "review_notes", "reviewed_by", "reviewed_at")}),
        ("Provisioning", {"fields": ("tenant", "provision_error", "created_at", "updated_at")}),
    )
    actions = ["reject_requests"]

    @admin.action(description="Reject selected requests (submitted only)")
    def reject_requests(self, request, queryset):
        updated = queryset.filter(status=TenantProvisioning.Status.SUBMITTED).update(
            status=TenantProvisioning.Status.REJECTED,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        self.message_user(request, f"{updated} request(s) rejected.", messages.SUCCESS)

    def has_add_permission(self, request):
        # Requests come from the public onboarding API, not the admin.
        return False


@admin.register(PlatformAdmin)
class PlatformAdminAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "created_at")
    search_fields = ("user__email", "title")
    raw_id_fields = ("user",)
