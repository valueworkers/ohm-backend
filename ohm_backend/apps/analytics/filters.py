from datetime import date
from decimal import Decimal, InvalidOperation

import django_filters
from django.db.models import Q

from apps.billing.constants import InvoiceStatus
from apps.billing.models import Invoice

ALLOWED_EMPLOYEE_SORT_FIELDS = {
    "emp_id": "employee_profile__employee_id",
    "first_name": "first_name",
    "last_name": "last_name",
    "mobile_number": "mobile_number",
}

ALLOWED_PERIOD_SORT_FIELDS = {
    "start_date": "start_date",
    "end_date": "end_date",
    "calendar_days": "calendar_days",
    "days_present": "days_present",
    "days_absent": "days_absent",
    "base_salary": "base_salary",
    "salary": "salary",
    "total_salary": "total_payable_amount",
    "amount_paid": "paid_amount",
    "excess_balance": "excess_balance",
    "status": "tx_status",
    "payment_date": "payment_date",
}


def _parse_date(value, param_name):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(f"'{param_name}' must be YYYY-MM-DD.")


def _parse_decimal(value, param_name):
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError(f"'{param_name}' must be a valid number.")


def _parse_int(value, param_name):
    try:
        return int(value)
    except (ValueError, TypeError):
        raise ValueError(f"'{param_name}' must be an integer.")


def build_employee_filters(params: dict) -> tuple[Q, list[str]]:
    q = Q()
    errors = []

    if uid := params.get("user_id"):
        try:
            q &= Q(pk=_parse_int(uid, "user_id"))
        except ValueError as e:
            errors.append(str(e))

    if eid := params.get("emp_id"):
        q &= Q(employee_profile__employee_id=eid)

    if eid_c := params.get("emp_id__icontains"):
        q &= Q(employee_profile__employee_id__icontains=eid_c)

    if fn := params.get("first_name__icontains"):
        q &= Q(first_name__icontains=fn)

    if ln := params.get("last_name__icontains"):
        q &= Q(last_name__icontains=ln)

    if mob := params.get("mobile_number"):
        q &= Q(mobile_number=mob)

    if search := params.get("search"):
        q &= (
            Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(employee_profile__employee_id__icontains=search)
            | Q(mobile_number__icontains=search)
        )

    if status_val := params.get("status"):
        if status_val.lower() == "active":
            q &= Q(is_active=True)
        elif status_val.lower() == "inactive":
            q &= Q(is_active=False)

    return q, errors


def build_period_filters(params: dict) -> tuple[Q, list[str]]:
    q = Q()
    errors = []

    date_params = [
        ("start_date", "start_date"),
        ("start_date__gte", "start_date__gte"),
        ("start_date__lte", "start_date__lte"),
        ("end_date", "end_date"),
        ("end_date__gte", "end_date__gte"),
        ("end_date__lte", "end_date__lte"),
    ]
    for param, lookup in date_params:
        if val := params.get(param):
            try:
                q &= Q(**{lookup: _parse_date(val, param)})
            except ValueError as e:
                errors.append(str(e))

    decimal_range_params = [
        ("days_present__gte", "days_present__gte"),
        ("days_present__lte", "days_present__lte"),
        ("days_absent__gte", "days_absent__gte"),
        ("days_absent__lte", "days_absent__lte"),
        ("base_salary__gte", "base_salary__gte"),
        ("base_salary__lte", "base_salary__lte"),
        ("salary__gte", "salary__gte"),
        ("salary__lte", "salary__lte"),
        ("total_salary__gte", "total_payable_amount__gte"),
        ("total_salary__lte", "total_payable_amount__lte"),
        ("amount_paid__gte", "paid_amount__gte"),
        ("amount_paid__lte", "paid_amount__lte"),
    ]
    for param, lookup in decimal_range_params:
        if val := params.get(param):
            try:
                q &= Q(**{lookup: _parse_decimal(val, param)})
            except ValueError as e:
                errors.append(str(e))

    return q, errors


def build_sort(params: dict) -> tuple[list[str], list[str]]:
    errors = []
    order_by = []
    sort_dir = params.get("sort_dir", "asc").lower()

    if sort_dir not in ("asc", "desc"):
        errors.append("'sort_dir' must be 'asc' or 'desc'.")
        sort_dir = "asc"

    prefix = "-" if sort_dir == "desc" else ""
    all_sortable = {**ALLOWED_EMPLOYEE_SORT_FIELDS, **ALLOWED_PERIOD_SORT_FIELDS}

    raw_sort = params.get("sort_by", "")
    if raw_sort:
        for field in [f.strip() for f in raw_sort.split(",") if f.strip()]:
            if field not in all_sortable:
                errors.append(
                    f"'{field}' is not a sortable field. "
                    f"Allowed: {sorted(all_sortable.keys())}"
                )
            else:
                order_by.append(f"{prefix}{all_sortable[field]}")

    return order_by, errors


class TotalInvoiceFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=InvoiceStatus.choices)
    invoice_type = django_filters.CharFilter(method="filter_invoice_type")

    patient = django_filters.NumberFilter(field_name="patient__id")
    user = django_filters.NumberFilter(field_name="user__id")

    period_start_after = django_filters.DateTimeFilter(field_name="period_start", lookup_expr="gte")
    period_start_before = django_filters.DateTimeFilter(field_name="period_start", lookup_expr="lte")
    period_end_after = django_filters.DateTimeFilter(field_name="period_end", lookup_expr="gte")
    period_end_before = django_filters.DateTimeFilter(field_name="period_end", lookup_expr="lte")

    issue_date_after = django_filters.DateFilter(field_name="issue_date", lookup_expr="gte")
    issue_date_before = django_filters.DateFilter(field_name="issue_date", lookup_expr="lte")
    issued_date_after = django_filters.DateFilter(field_name="issue_date", lookup_expr="gte")
    issued_date_before = django_filters.DateFilter(field_name="issue_date", lookup_expr="lte")

    due_date_after = django_filters.DateFilter(field_name="due_date", lookup_expr="gte")
    due_date_before = django_filters.DateFilter(field_name="due_date", lookup_expr="lte")

    total_min = django_filters.NumberFilter(field_name="total_amount", lookup_expr="gte")
    total_max = django_filters.NumberFilter(field_name="total_amount", lookup_expr="lte")
    remaining_min = django_filters.NumberFilter(field_name="remaining_amount", lookup_expr="gte")
    remaining_max = django_filters.NumberFilter(field_name="remaining_amount", lookup_expr="lte")

    secondary_order = django_filters.NumberFilter(field_name="secondary_order__id")
    ternary_order = django_filters.NumberFilter(field_name="ternary_order__id")

    class Meta:
        model = Invoice
        fields = []

    def filter_invoice_type(self, queryset, name, value):
        val = value.upper()
        if val == "SECONDARY":
            return queryset.filter(secondary_order__isnull=False)
        if val == "TERNARY":
            return queryset.filter(ternary_order__isnull=False)
        return queryset
