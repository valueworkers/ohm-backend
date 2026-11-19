import logging
from datetime import date

from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from apps.platform.tasks import TenantTask

from .models import Attendance, AttendanceStatus, SalaryReport
from .utils import PayrollCalculator

logger = logging.getLogger(__name__)
User = get_user_model()


@shared_task(base=TenantTask)
def mark_attendance_present():
    """Create today's missing attendance records and refresh affected payroll."""
    present_status = AttendanceStatus.objects.filter(code="PRESENT", is_active=True).first()
    if not present_status:
        return {"status": "error", "message": "Active PRESENT attendance status was not found."}

    today = date.today()
    if hasattr(User.objects, "employees"):
        users_qs = User.objects.employees()
    else:
        users_qs = User.objects.filter(is_staff=True)

    if hasattr(User, "is_deleted"):
        users_qs = users_qs.filter(is_deleted=False)

    user_ids = list(users_qs.values_list("id", flat=True))
    existing_ids = set(
        Attendance.objects.filter(user_id__in=user_ids, date=today).values_list("user_id", flat=True)
    )
    new_records = [
        Attendance(user_id=uid, date=today, status=present_status)
        for uid in user_ids
        if uid not in existing_ids
    ]
    Attendance.objects.bulk_create(new_records, batch_size=5000)

    for user in User.objects.filter(id__in=[r.user_id for r in new_records]):
        PayrollCalculator(user).refresh_salary_reports()

    return {"status": "success", "message": f"Created {len(new_records)} attendance record(s)."}


@shared_task(base=TenantTask, bind=True, max_retries=3, default_retry_delay=10)
def refresh_salary_reports_task(self, user_id):
    """
    Runs PayrollCalculator(user).refresh_salary_reports() in the background.
    """
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return
    try:
        PayrollCalculator(user).refresh_salary_reports()
        return {
            "status": "success",
            "message": f"Salary reports updated successfully for user {user.id}.",
        }
    except Exception as exc:
        raise self.retry(exc=exc)


def queue_salary_report_refresh(user_id, countdown=5):
    """
    Debounced enqueue. If a refresh for this user is already scheduled within
    the debounce window, this is a no-op.
    """
    lock_key = f"payroll:refresh-scheduled:{user_id}"
    if cache.add(lock_key, "1", timeout=countdown + 1):
        from django.db import connection
        schema_name = getattr(connection, "schema_name", "public")
        refresh_salary_reports_task.apply_async(
            args=[user_id],
            kwargs={"schema_name": schema_name},
            countdown=countdown,
        )


@shared_task(base=TenantTask)
def finalize_salary_report_task(report_id):
    """Locks one report so future refresh runs skip it."""
    SalaryReport.objects.filter(pk=report_id).update(is_finalized=True)


@shared_task(base=TenantTask)
def finalize_closed_period_reports(before_date=None):
    cutoff = before_date or timezone.localdate()
    updated = SalaryReport.objects.filter(end_date__lt=cutoff, is_finalized=False).update(
        is_finalized=True
    )
    return updated
