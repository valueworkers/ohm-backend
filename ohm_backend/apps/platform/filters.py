# apps/platform/filters.py  (NEW)   pip install django-filter  ->  INSTALLED_APPS += ["django_filters"]
from datetime import timedelta

import django_filters as df
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import Quote


def latest_revisions(qs):
    """Keep only the newest revision of each quote number."""
    newer = Quote.objects.filter(number=OuterRef("number"), revision__gt=OuterRef("revision"))
    return qs.annotate(_has_newer=Exists(newer)).filter(_has_newer=False)


class QuoteFilter(df.FilterSet):
    # ?status=sent&status=draft
    status = df.MultipleChoiceFilter(choices=Quote.Status.choices)
    supply_type = df.MultipleChoiceFilter(choices=Quote.SupplyType.choices)   # intra_state / intra_ut / inter_state
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