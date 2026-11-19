"""
apps/billing/admin.py
─────────────────────
Django admin registrations for all tenant-scoped billing, quotation, order, and invoice models.
"""

from django.contrib import admin, messages
from django.db import connection
from django_tenants.utils import get_public_schema_name

from .models import (
    ContactBooking,
    DocumentSequence,
    Invoice,
    InvoiceItem,
    InvoiceSequence,
    Location,
    Package,
    Patient,
    PatientDocument,
    PatientDocumentFile,
    Payment,
    PrimaryOrder,
    Quote,
    QuoteCounter,
    QuoteEvent,
    QuoteLine,
    QuoteTaxSettings,
    SecondaryOrder,
    TernaryOrder,
)


class TenantModelAdmin(admin.ModelAdmin):
    """Base ModelAdmin for tenant-scoped models to prevent errors in public schema."""

    def has_module_permission(self, request):
        if connection.schema_name == get_public_schema_name():
            return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if connection.schema_name == get_public_schema_name():
            return False
        return super().has_view_permission(request, obj)


# ---------------------------------------------------------------------------
# Inlines
# ---------------------------------------------------------------------------

class PatientDocumentFileInline(admin.TabularInline):
    model = PatientDocumentFile
    extra = 0
    readonly_fields = ("uploaded_at",)


class PatientDocumentInline(admin.StackedInline):
    model = PatientDocument
    extra = 0
    show_change_link = True
    raw_id_fields = ("uploaded_by",)


class SecondaryOrderInline(admin.TabularInline):
    model = SecondaryOrder
    extra = 0
    show_change_link = True
    readonly_fields = ("order_id", "subtotal", "status", "created_at")
    fields = (
        "order_id", "package", "service", "venue", "booking_type",
        "start_datetime", "end_datetime",
        "subtotal", "status", "is_registration_fee", "status_locked",
    )
    raw_id_fields = ("package", "service", "venue")


class TernaryOrderInline(admin.TabularInline):
    model = TernaryOrder
    extra = 0
    show_change_link = True
    readonly_fields = ("order_id", "subtotal", "status")


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0
    readonly_fields = ("amount", "taxable_value", "cgst_amount", "sgst_amount", "igst_amount")


class SecondaryInvoiceInline(admin.TabularInline):
    model = Invoice
    fk_name = "secondary_order"
    extra = 0
    show_change_link = True
    readonly_fields = (
        "invoice_number", "total_amount", "paid_amount", "remaining_amount", "status"
    )
    fields = (
        "invoice_number", "total_amount", "paid_amount", "remaining_amount", "status"
    )


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = ("reference", "created_at")
    raw_id_fields = ("patient",)


class QuoteLineInline(admin.TabularInline):
    model = QuoteLine
    extra = 0
    readonly_fields = ("taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "line_total")


class QuoteEventInline(admin.TabularInline):
    model = QuoteEvent
    extra = 0
    readonly_fields = ("kind", "detail", "actor", "at")


# ---------------------------------------------------------------------------
# ModelAdmins
# ---------------------------------------------------------------------------

@admin.register(Location)
class LocationAdmin(TenantModelAdmin):
    list_display = ("building_name", "locality", "city", "state", "postal_code", "location_type", "user")
    list_filter = ("location_type", "city", "state")
    search_fields = ("building_name", "address_line1", "address_line2", "locality", "city", "state", "postal_code")
    raw_id_fields = ("user",)


@admin.register(Package)
class PackageAdmin(TenantModelAdmin):
    list_display = ("name", "owner", "package_type", "period", "price", "registration_fees", "is_active", "content_type", "created_at")
    list_filter = ("package_type", "period", "is_active", "created_at")
    search_fields = ("name", "description", "owner__email", "owner__first_name")
    raw_id_fields = ("owner",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Patient)
class PatientAdmin(TenantModelAdmin):
    list_display = (
        "patient_id", "get_full_name", "phone", "email",
        "gender", "blood_group", "is_active", "is_deleted",
        "is_probono", "registration_date",
    )
    list_filter = ("gender", "blood_group", "is_active", "is_deleted", "is_probono", "registration_date")
    search_fields = ("patient_id", "first_name", "last_name", "phone", "email")
    readonly_fields = ("patient_id", "registration_date", "updated_at")
    raw_id_fields = ("registered_by",)
    inlines = [PatientDocumentInline]
    date_hierarchy = "registration_date"
    actions = ("soft_delete_patients", "restore_patients")

    @admin.display(description="Full Name")
    def get_full_name(self, obj):
        return obj.get_full_name()

    @admin.action(description="Soft delete selected patients")
    def soft_delete_patients(self, request, queryset):
        count = queryset.update(is_active=False, is_deleted=True)
        self.message_user(request, f"{count} patient(s) soft deleted.", messages.SUCCESS)

    @admin.action(description="Restore soft-deleted patients")
    def restore_patients(self, request, queryset):
        count = queryset.update(is_active=True, is_deleted=False)
        self.message_user(request, f"{count} patient(s) restored.", messages.SUCCESS)


@admin.register(PatientDocument)
class PatientDocumentAdmin(TenantModelAdmin):
    list_display = ("patient", "title", "uploaded_by", "created_at")
    list_filter = ("created_at",)
    search_fields = ("patient__first_name", "patient__last_name", "patient__patient_id", "title")
    raw_id_fields = ("patient", "uploaded_by")
    readonly_fields = ("created_at", "updated_at")
    inlines = [PatientDocumentFileInline]


@admin.register(PatientDocumentFile)
class PatientDocumentFileAdmin(TenantModelAdmin):
    list_display = ("id", "document", "file", "uploaded_at")
    raw_id_fields = ("document",)
    readonly_fields = ("uploaded_at",)


@admin.register(ContactBooking)
class ContactBookingAdmin(TenantModelAdmin):
    list_display = ("pk", "patient", "service", "start_date", "end_date", "booked_by", "created_at")
    list_filter = ("start_date", "end_date", "created_at")
    search_fields = ("patient__first_name", "patient__last_name", "patient__patient_id", "service__name")
    raw_id_fields = ("patient", "service", "booked_by")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "start_date"


@admin.register(PrimaryOrder)
class PrimaryOrderAdmin(TenantModelAdmin):
    list_display = (
        "order_id", "patient", "user", "booking_entity",
        "booking_type", "status", "total_bill",
        "start_datetime", "end_datetime", "auto_continue", "created_at",
    )
    list_filter = ("booking_entity", "booking_type", "status", "auto_continue", "created_at")
    search_fields = ("order_id", "patient__first_name", "patient__last_name", "patient__patient_id", "user__email")
    raw_id_fields = ("user", "patient")
    readonly_fields = ("order_id", "total_bill", "created_at", "updated_at")
    date_hierarchy = "created_at"
    inlines = [SecondaryOrderInline]


@admin.register(SecondaryOrder)
class SecondaryOrderAdmin(TenantModelAdmin):
    list_display = (
        "order_id", "primary_order", "package", "venue", "service",
        "booking_type", "status", "subtotal",
        "is_registration_fee", "start_datetime", "end_datetime",
    )
    list_filter = ("status", "booking_type", "booking_entity", "status_locked", "auto_continue", "is_registration_fee")
    search_fields = ("order_id", "primary_order__order_id", "primary_order__patient__first_name", "primary_order__patient__last_name")
    raw_id_fields = ("primary_order", "venue", "service", "package")
    readonly_fields = ("order_id", "subtotal", "created_at", "updated_at")
    inlines = [SecondaryInvoiceInline, TernaryOrderInline]


@admin.register(TernaryOrder)
class TernaryOrderAdmin(TenantModelAdmin):
    list_display = (
        "order_id", "secondary_order", "booking_entity",
        "booking_type", "subtotal", "status", "start_datetime", "end_datetime",
    )
    list_filter = ("booking_entity", "booking_type", "status", "created_at")
    search_fields = ("order_id", "secondary_order__order_id")
    raw_id_fields = ("secondary_order",)
    readonly_fields = ("order_id", "subtotal", "created_at", "updated_at")


# ---------------------------------------------------------------------------
# Quotation Admin
# ---------------------------------------------------------------------------

@admin.register(QuoteTaxSettings)
class QuoteTaxSettingsAdmin(TenantModelAdmin):
    list_display = ("state_name", "state_code", "gstin", "updated_at")
    search_fields = ("state_name", "state_code", "gstin")
    readonly_fields = ("updated_at",)


@admin.register(Quote)
class QuoteAdmin(TenantModelAdmin):
    list_display = (
        "number", "revision", "company", "attention", "buyer_email",
        "status", "quote_date", "valid_until", "total", "created_by", "created_at",
    )
    list_filter = ("status", "booking_entity", "supply_type", "quote_date")
    search_fields = ("number", "company", "attention", "buyer_email", "gstin")
    raw_id_fields = ("venue", "service", "resource", "patient", "created_by")
    readonly_fields = (
        "accept_token", "sent_at", "accepted_at", "created_at",
        "subtotal", "discount_total", "taxable_amount", "total",
        "cgst", "sgst", "igst",
    )
    date_hierarchy = "quote_date"
    inlines = [QuoteLineInline, QuoteEventInline]
    actions = ("recalculate_selected_quotes",)

    @admin.action(description="Recalculate GST line items and totals for selected quotes")
    def recalculate_selected_quotes(self, request, queryset):
        from .services import recalculate_quote
        count = 0
        for quote in queryset:
            try:
                recalculate_quote(quote)
                count += 1
            except Exception as e:
                self.message_user(request, f"Error recalculating quote {quote.number}: {e}", messages.ERROR)
        if count:
            self.message_user(request, f"{count} quote(s) recalculated successfully.", messages.SUCCESS)


@admin.register(QuoteLine)
class QuoteLineAdmin(TenantModelAdmin):
    list_display = ("id", "quote", "item", "qty", "unit_price", "discount_percent", "gst_rate", "line_total")
    search_fields = ("item", "hsn_sac", "quote__number")
    raw_id_fields = ("quote",)


@admin.register(QuoteEvent)
class QuoteEventAdmin(TenantModelAdmin):
    list_display = ("id", "quote", "kind", "actor", "at")
    list_filter = ("kind", "at")
    search_fields = ("quote__number", "detail", "actor__email")
    raw_id_fields = ("quote", "actor")
    readonly_fields = ("at",)


# ---------------------------------------------------------------------------
# Invoice & Payment Admin
# ---------------------------------------------------------------------------

@admin.register(Invoice)
class InvoiceAdmin(TenantModelAdmin):
    list_display = (
        "invoice_number", "customer_name", "invoice_type",
        "total_amount", "paid_amount", "remaining_amount",
        "status", "issue_date", "due_date",
    )
    list_filter = ("status", "invoice_type_tag", "booking_entity", "is_interstate", "issue_date")
    search_fields = ("invoice_number", "customer_name", "attention", "buyer_email", "gstin")
    raw_id_fields = ("venue", "service", "resource", "patient", "user", "quote", "secondary_order", "ternary_order")
    readonly_fields = (
        "invoice_number", "total_amount", "remaining_amount",
        "sent_at", "created_at", "updated_at",
    )
    date_hierarchy = "issue_date"
    inlines = [InvoiceItemInline, PaymentInline]
    actions = ("issue_selected_invoices", "void_selected_invoices", "recalculate_selected_invoices")

    @admin.display(description="Type")
    def invoice_type(self, obj):
        return obj.invoice_type

    @admin.action(description="Issue selected draft invoices")
    def issue_selected_invoices(self, request, queryset):
        from .services import issue_invoice
        count = 0
        for inv in queryset:
            try:
                issue_invoice(inv)
                count += 1
            except Exception as e:
                self.message_user(request, f"Error issuing invoice {inv.invoice_number}: {e}", messages.ERROR)
        if count:
            self.message_user(request, f"{count} invoice(s) issued successfully.", messages.SUCCESS)

    @admin.action(description="Void selected invoices")
    def void_selected_invoices(self, request, queryset):
        from .services import void_invoice
        count = 0
        for inv in queryset:
            try:
                void_invoice(inv)
                count += 1
            except Exception as e:
                self.message_user(request, f"Error voiding invoice {inv.invoice_number}: {e}", messages.ERROR)
        if count:
            self.message_user(request, f"{count} invoice(s) marked as VOID.", messages.SUCCESS)

    @admin.action(description="Recalculate line items and payments for selected invoices")
    def recalculate_selected_invoices(self, request, queryset):
        count = 0
        for inv in queryset:
            inv.recalculate_from_items()
            inv.recalculate_payments()
            count += 1
        self.message_user(request, f"{count} invoice(s) recalculated successfully.", messages.SUCCESS)


@admin.register(InvoiceItem)
class InvoiceItemAdmin(TenantModelAdmin):
    list_display = ("id", "invoice", "description", "quantity", "unit_price", "discount_amount", "gst_rate", "amount")
    search_fields = ("description", "hsn_sac", "invoice__invoice_number")
    raw_id_fields = ("invoice",)


@admin.register(Payment)
class PaymentAdmin(TenantModelAdmin):
    list_display = (
        "id", "invoice", "patient", "amount", "method",
        "reference", "paid_date", "is_verified", "mapping_status",
    )
    list_filter = ("method", "is_verified", "mapping_status", "paid_date")
    search_fields = ("reference", "invoice__invoice_number", "patient__first_name", "patient__last_name")
    raw_id_fields = ("invoice", "patient")
    readonly_fields = ("reference", "created_at", "updated_at")
    date_hierarchy = "paid_date"
    actions = ("verify_selected_payments", "unverify_selected_payments")

    @admin.action(description="Mark selected payments as verified")
    def verify_selected_payments(self, request, queryset):
        count = 0
        for pmt in queryset:
            if pmt.verify():
                count += 1
        self.message_user(request, f"{count} payment(s) verified.", messages.SUCCESS)

    @admin.action(description="Mark selected payments as unverified")
    def unverify_selected_payments(self, request, queryset):
        count = 0
        for pmt in queryset:
            if pmt.unverify():
                count += 1
        self.message_user(request, f"{count} payment(s) marked as unverified.", messages.SUCCESS)


# ---------------------------------------------------------------------------
# Counters & Document Sequences
# ---------------------------------------------------------------------------

@admin.register(InvoiceSequence)
class InvoiceSequenceAdmin(TenantModelAdmin):
    list_display = ("year", "last_number")


@admin.register(QuoteCounter)
class QuoteCounterAdmin(TenantModelAdmin):
    list_display = ("period", "last_seq")


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(TenantModelAdmin):
    list_display = ("prefix", "year", "last_value")
