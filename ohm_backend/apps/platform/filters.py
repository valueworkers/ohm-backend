# apps/platform/filters.py  (NEW)   pip install django-filter  ->  INSTALLED_APPS += ["django_filters"]
from datetime import timedelta

import django_filters as df
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .models import Quote, Invoice, Booking, Payment


def latest_revisions(qs):
    """Keep only the newest revision of each quote number."""
    newer = Quote.objects.filter(number=OuterRef("number"), revision__gt=OuterRef("revision"))
    return qs.annotate(_has_newer=Exists(newer)).filter(_has_newer=False)


class QuoteFilter(df.FilterSet):
    # ?status=sent&status=draft
    status = df.MultipleChoiceFilter(choices=Quote.Status.choices)
    supply_type = df.MultipleChoiceFilter(choices=Quote.SupplyType.choices)   # intra_state / intra_ut / inter_state
    tenant = df.NumberFilter(field_name="tenant_id")
    number = df.CharFilter(lookup_expr="icontains")
    company = df.CharFilter(lookup_expr="icontains")
    buyer_email = df.CharFilter(lookup_expr="icontains")
    gstin = df.CharFilter(lookup_expr="iexact")
    state = df.CharFilter(lookup_expr="iexact")
    state_code = df.CharFilter(lookup_expr="exact")
    quote_date_after = df.DateFilter(field_name="quote_date", lookup_expr="gte")
    quote_date_before = df.DateFilter(field_name="quote_date", lookup_expr="lte")
    total_min = df.NumberFilter(field_name="total", lookup_expr="gte")
    total_max = df.NumberFilter(field_name="total", lookup_expr="lte")
    line_item = df.CharFilter(field_name="lines__item", lookup_expr="icontains", distinct=True)
    hsn_sac = df.CharFilter(field_name="lines__hsn_sac", lookup_expr="startswith", distinct=True)
    range = df.NumberFilter(method="filter_range", label="Quotes from the last N days")
    latest_only = df.BooleanFilter(method="filter_latest_only", label="Hide superseded revisions")

    class Meta:
        model = Quote
        fields = []

    def filter_range(self, qs, name, value):
        return qs.filter(quote_date__gte=timezone.localdate() - timedelta(days=int(value)))

    def filter_latest_only(self, qs, name, value):
        return latest_revisions(qs) if value else qs


class InvoiceFilter(df.FilterSet):
    status = df.CharFilter(method="filter_status")
    issue_date_from = df.DateFilter(field_name="issue_date", lookup_expr="gte")
    issue_date_to = df.DateFilter(field_name="issue_date", lookup_expr="lte")
    due_date_from = df.DateFilter(field_name="due_date", lookup_expr="gte")
    due_date_to = df.DateFilter(field_name="due_date", lookup_expr="lte")
    total_min = df.NumberFilter(field_name="total", lookup_expr="gte")
    total_max = df.NumberFilter(field_name="total", lookup_expr="lte")
    overdue = df.BooleanFilter(method="filter_overdue")
    tenant = df.NumberFilter(field_name="tenant_id")
    customer_name = df.CharFilter(lookup_expr="icontains")
    buyer_email = df.CharFilter(lookup_expr="icontains")
    gstin = df.CharFilter(lookup_expr="iexact")
    number = df.CharFilter(lookup_expr="icontains")
    state_code = df.CharFilter(lookup_expr="exact")
    is_interstate = df.BooleanFilter()
    currency = df.CharFilter(lookup_expr="iexact")

    booking = df.NumberFilter(field_name="booking_id")

    class Meta:
        model = Invoice
        fields = ["tenant", "subscription", "quotation", "booking", "state_code", "is_interstate", "currency"]

    def filter_status(self, qs, name, value):
        values = self.request.GET.getlist("status") if self.request else []
        final_values = set()
        for v in values:
            for item in v.split(","):
                item = item.strip()
                if item:
                    final_values.add(item)
        if not final_values and value:
            final_values = {v.strip() for v in str(value).split(",") if v.strip()}
        return qs.filter(status__in=final_values) if final_values else qs

    def filter_overdue(self, qs, name, value):
        cond = Q(
            status__in=[Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.OVERDUE],
            due_date__lt=timezone.localdate(),
        )
        return qs.filter(cond) if value else qs.exclude(cond)


class BookingFilter(df.FilterSet):
    tenant = df.NumberFilter(field_name="tenant_id")
    status = df.ChoiceFilter(choices=Booking.Status.choices)
    booking_type = df.ChoiceFilter(choices=Booking.BookingType.choices)
    booking_id = df.CharFilter(lookup_expr="icontains")
    title = df.CharFilter(lookup_expr="icontains")
    start_date_from = df.DateTimeFilter(field_name="start_datetime", lookup_expr="gte")
    start_date_to = df.DateTimeFilter(field_name="start_datetime", lookup_expr="lte")

    class Meta:
        model = Booking
        fields = ["tenant", "status", "booking_type", "plan", "quote"]


class PaymentFilter(df.FilterSet):
    tenant = df.NumberFilter(field_name="tenant_id")
    invoice = df.NumberFilter(field_name="invoice_id")
    method = df.ChoiceFilter(choices=Payment.Method.choices)
    status = df.ChoiceFilter(choices=Payment.Status.choices)
    gateway = df.CharFilter(lookup_expr="icontains")
    reference = df.CharFilter(lookup_expr="icontains")
    paid_after = df.DateTimeFilter(field_name="paid_at", lookup_expr="gte")
    paid_before = df.DateTimeFilter(field_name="paid_at", lookup_expr="lte")

    class Meta:
        model = Payment
        fields = ["tenant", "invoice", "method", "status", "gateway"]
