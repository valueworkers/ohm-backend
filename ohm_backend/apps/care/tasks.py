import logging
from celery import shared_task
from django_tenants.utils import get_public_schema_name, schema_context

from .constants import BookingStatus
from .models import SecondaryOrder, TernaryOrder
from .services import (
    AttendanceService,
    CareInvoiceService,
    MedicareInvoiceService,
    OrderLifecycleService,
)

logger = logging.getLogger(__name__)

INVOICE_BILLABLE_STATUSES = [
    BookingStatus.BOOKED,
    BookingStatus.IN_PROGRESS,
    BookingStatus.UNFULFILLED,
    BookingStatus.PARTIALLY_FULFILLED,
    BookingStatus.FULFILLED,
]


@shared_task
def mark_attendance_present():
    """Create today's missing attendance records for active staff in the current schema."""
    result = AttendanceService.mark_staff_present_today()
    logger.info("mark_attendance_present completed: %s", result)
    return result


@shared_task
def update_statuses_by_time():
    """Advance order statuses (BOOKED -> IN_PROGRESS -> COMPLETED) based on current wall time."""
    result = OrderLifecycleService.update_order_statuses_by_time()
    logger.info("update_statuses_by_time completed: %s", result)
    return result


@shared_task
def trigger_auto_continue_secondary_orders():
    """Nightly auto-continuation: creates next billing cycle for expiring orders."""
    result = OrderLifecycleService.auto_continue_expiring_orders()
    logger.info("trigger_auto_continue_secondary_orders completed: %s", result)
    return result


@shared_task
def reconcile_invoices():
    """
    Reconciliation sweep for Secondary & Ternary Orders created/updated via bulk operations
    that bypassed post_save signals. Generates missing billing invoices.
    """
    from apps.billing.models import Invoice

    # Find SecondaryOrders without an invoice
    secondaries = SecondaryOrder.objects.filter(
        status__in=INVOICE_BILLABLE_STATUSES,
    )
    sec_created, sec_failed = 0, 0
    for sec in secondaries:
        has_inv = Invoice.objects.filter(notes__contains=f"SecondaryOrder:{sec.order_id}").exists()
        if not has_inv:
            try:
                MedicareInvoiceService.generate_invoice_for_secondary(sec)
                sec_created += 1
            except Exception as e:
                sec_failed += 1
                logger.exception("Failed reconciling invoice for SecondaryOrder %s: %s", sec.pk, e)

    # Find TernaryOrders without an invoice
    ternaries = TernaryOrder.objects.filter(
        status__in=INVOICE_BILLABLE_STATUSES,
    )
    ter_created, ter_failed = 0, 0
    for ter in ternaries:
        has_inv = Invoice.objects.filter(notes__contains=f"TernaryOrder:{ter.order_id}").exists()
        if not has_inv:
            try:
                MedicareInvoiceService.generate_invoice_for_ternary(ter)
                ter_created += 1
            except Exception as e:
                ter_failed += 1
                logger.exception("Failed reconciling invoice for TernaryOrder %s: %s", ter.pk, e)

    summary = {
        "secondary_invoices_created": sec_created,
        "secondary_failed": sec_failed,
        "ternary_invoices_created": ter_created,
        "ternary_failed": ter_failed,
    }
    logger.info("reconcile_invoices completed: %s", summary)
    return summary


@shared_task
def run_care_tenant_beat_maintenance():
    """
    Tenant-aware Celery Beat runner:
    Loops over all active tenants (excluding public) that have the 'care' or 'medicare' feature,
    and runs the maintenance tasks inside schema_context.
    """
    from apps.platform.models import Tenant

    public_name = get_public_schema_name()
    with schema_context(public_name):
        active_tenants = list(
            Tenant.objects.filter(status=Tenant.Status.ACTIVE)
            .exclude(schema_name=public_name)
        )

    results = {}
    for tenant in active_tenants:
        if tenant.has_feature("care") or tenant.has_feature("medicare"):
            logger.info("Executing Care maintenance for tenant schema: %s", tenant.schema_name)
            with schema_context(tenant.schema_name):
                try:
                    att_res = mark_attendance_present()
                    status_res = update_statuses_by_time()
                    continue_res = trigger_auto_continue_secondary_orders()
                    reconcile_res = reconcile_invoices()
                    results[tenant.schema_name] = {
                        "attendance": att_res,
                        "status_update": status_res,
                        "auto_continue": continue_res,
                        "reconciliation": reconcile_res,
                    }
                except Exception as e:
                    logger.exception("Care maintenance error in schema %s: %s", tenant.schema_name, e)
                    results[tenant.schema_name] = {"error": str(e)}

    return results


# Backwards-compatible alias
run_medicare_tenant_beat_maintenance = run_care_tenant_beat_maintenance
