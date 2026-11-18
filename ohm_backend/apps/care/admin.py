from django.contrib import admin

from .models import (
    Attendance,
    AttendanceStatus,
    TenantPaymentConfig,
)


@admin.register(TenantPaymentConfig)
class TenantPaymentConfigAdmin(admin.ModelAdmin):
    list_display = ["razorpay_key_id", "currency", "is_deleted", "updated_at"]


@admin.register(AttendanceStatus)
class AttendanceStatusAdmin(admin.ModelAdmin):
    list_display = ["code", "label", "is_deleted"]


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ["user", "date", "status_record", "duration"]
    list_filter = ["date", "status_record"]
