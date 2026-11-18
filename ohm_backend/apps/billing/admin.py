"""
apps/billing/admin.py
─────────────────────
Django admin registrations for all billing, quotation, order, and invoice models.
"""

from django.apps import apps
from django.contrib import admin

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

_HAS_VENUE_MANAGER = apps.is_installed("venue_manager")


# ---------------------------------------------------------------------------
# Inlines
# ---------------------------------------------------------------------------

class PatientDocumentFileInline(admin.TabularInline):
    model = PatientDocumentFile
    extra = 0


class PatientDocumentInline(admin.StackedInline):
    model = PatientDocument
    extra = 0
    show_change_link = True


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


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = ("reference", "created_at")


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
class LocationAdmin(admin.ModelAdmin):
    list_display = ("building_name", "city", "state", "location_type", "user")
    list_filter = ("location_type", "city", "state")
    search_fields = ("building_name", "address_line1", "city", "state", "postal_code")


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = ("name", "package_type", "period", "price", "registration_fees", "is_active")
    list_filter = ("package_type", "period", "is_active")
    search_fields = ("name",)


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = (
        "patient_id", "get_full_name", "phone", "gender",
        "is_active", "is_deleted", "registration_date",
    )
    list_filter = ("gender", "is_active", "is_deleted", "is_probono")
    search_fields = ("first_name", "last_name", "phone", "email", "patient_id")
    readonly_fields = ("patient_id", "registration_date", "updated_at")
    inlines = [PatientDocumentInline]

    @admin.display(description="Full Name")
    def get_full_name(self, obj):
        return obj.get_full_name()


@admin.register(PatientDocument)
class PatientDocumentAdmin(admin.ModelAdmin):
    list_display = ("patient", "title", "uploaded_by", "created_at")
    search_fields = ("patient__first_name", "patient__last_name", "title")
    inlines = [PatientDocumentFileInline]


if _HAS_VENUE_MANAGER:
    @admin.register(ContactBooking)
    class ContactBookingAdmin(admin.ModelAdmin):
        list_display = ("pk", "patient", "service", "start_date", "end_date", "booked_by")
        list_filter = ("start_date",)
        search_fields = ("patient__first_name", "patient__last_name")
        date_hierarchy = "start_date"


@admin.register(PrimaryOrder)
class PrimaryOrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_id", "patient", "booking_type", "status",
        "total_bill", "start_datetime", "end_datetime",
    )
    list_filter = ("booking_type", "status", "booking_entity", "auto_continue")
    search_fields = ("order_id", "patient__first_name", "patient__last_name")
    readonly_fields = ("order_id", "total_bill", "created_at", "updated_at")
    date_hierarchy = "created_at"
    inlines = [SecondaryOrderInline]


@admin.register(SecondaryOrder)
class SecondaryOrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_id", "primary_order", "package", "service", "venue", "booking_type", "status",
        "subtotal", "start_datetime", "end_datetime", "is_registration_fee",
    )
    list_filter = ("status", "booking_type", "is_registration_fee", "status_locked")
    search_fields = ("order_id", "primary_order__order_id")
    readonly_fields = ("order_id", "subtotal", "created_at", "updated_at")
    inlines = [SecondaryInvoiceInline] + ([TernaryOrderInline] if _HAS_VENUE_MANAGER else [])


@admin.register(TernaryOrder)
class TernaryOrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_id", "secondary_order", "booking_type",
        "subtotal", "status", "start_datetime", "end_datetime",
    )
    list_filter = ("booking_type", "status", "booking_entity")
    search_fields = ("order_id",)
    readonly_fields = ("order_id", "subtotal", "created_at", "updated_at")


# ---------------------------------------------------------------------------
# Quotation Admin
# ---------------------------------------------------------------------------

@admin.register(QuoteTaxSettings)
class QuoteTaxSettingsAdmin(admin.ModelAdmin):
    list_display = ("state_name", "state_code", "gstin", "updated_at")


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = (
        "number", "revision", "company", "status",
        "quote_date", "valid_until", "total", "created_by",
    )
    list_filter = ("status", "booking_entity", "supply_type")
    search_fields = ("number", "company", "buyer_email", "gstin")
    readonly_fields = (
        "accept_token", "sent_at", "accepted_at", "created_at",
        "subtotal", "discount_total", "taxable_amount", "total",
    )
    date_hierarchy = "quote_date"
    inlines = [QuoteLineInline, QuoteEventInline]


# ---------------------------------------------------------------------------
# Invoice Admin
# ---------------------------------------------------------------------------

@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_number", "customer_name", "invoice_type",
        "total_amount", "paid_amount", "remaining_amount",
        "status", "issue_date", "due_date",
    )
    list_filter = ("status", "invoice_type_tag", "booking_entity", "is_interstate")
    search_fields = ("invoice_number", "customer_name", "buyer_email", "gstin")
    readonly_fields = (
        "invoice_number", "total_amount", "remaining_amount",
        "sent_at", "created_at", "updated_at",
    )
    date_hierarchy = "issue_date"
    inlines = [InvoiceItemInline, PaymentInline]

    @admin.display(description="Type")
    def invoice_type(self, obj):
        return obj.invoice_type


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id", "invoice", "patient", "amount", "method",
        "paid_date", "is_verified", "mapping_status",
    )
    list_filter = ("method", "is_verified", "mapping_status")
    search_fields = ("reference", "invoice__invoice_number")
    readonly_fields = ("reference", "created_at", "updated_at")
    date_hierarchy = "paid_date"


@admin.register(InvoiceSequence)
class InvoiceSequenceAdmin(admin.ModelAdmin):
    list_display = ("year", "last_number")


@admin.register(QuoteCounter)
class QuoteCounterAdmin(admin.ModelAdmin):
    list_display = ("period", "last_seq")


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(admin.ModelAdmin):
    list_display = ("prefix", "year", "last_value")
