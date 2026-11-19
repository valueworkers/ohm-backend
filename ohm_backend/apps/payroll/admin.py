from django.contrib import admin

from apps.platform.admin_base import TenantModelAdmin
from .models import (
    Attendance,
    AttendanceStatus,
    SalaryReport,
    SalaryStructure,
    SalaryTransaction,
)


@admin.register(AttendanceStatus)
class AttendanceStatusAdmin(TenantModelAdmin):
    list_display = ("owner", "label", "code", "is_active")
    list_filter = ("is_active",)
    search_fields = ("label", "code", "owner__email")


@admin.register(Attendance)
class AttendanceAdmin(TenantModelAdmin):
    list_display = ("user", "date", "status", "duration", "created_at")
    list_filter = ("status", "date")
    search_fields = ("user__first_name", "user__last_name", "user__email")
    list_select_related = ("user", "status")


@admin.register(SalaryStructure)
class SalaryStructureAdmin(TenantModelAdmin):
    list_display = ("user", "salary_type", "change_type", "amount", "final_salary", "effective_from")
    list_filter = ("salary_type", "change_type", "effective_from")
    search_fields = ("user__first_name", "user__last_name", "user__email")


@admin.register(SalaryReport)
class SalaryReportAdmin(TenantModelAdmin):
    list_display = ("user", "start_date", "end_date", "total_payable_amount", "paid_amount", "is_finalized")
    list_filter = ("is_finalized", "start_date")
    search_fields = ("user__first_name", "user__last_name", "user__email")


@admin.register(SalaryTransaction)
class SalaryTransactionAdmin(TenantModelAdmin):
    list_display = ("transaction_id", "user", "amount_paid", "payment_method", "status", "paid_at")
    list_filter = ("status", "payment_method")
    search_fields = ("transaction_id", "user__first_name", "user__last_name", "payment_reference")
