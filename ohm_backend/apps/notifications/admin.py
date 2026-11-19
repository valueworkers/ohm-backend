from django.contrib import admin

from apps.platform.admin_base import TenantModelAdmin
from .models import Notification, NotificationTemplate


@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(TenantModelAdmin):
    list_display = ("name", "channel", "category", "owner", "is_active", "created_at")
    list_filter = ("channel", "category", "is_active")
    search_fields = ("name", "subject", "body")
    ordering = ("-created_at",)


@admin.register(Notification)
class NotificationAdmin(TenantModelAdmin):
    list_display = (
        "title",
        "channel",
        "category",
        "status",
        "recipient",
        "owner",
        "created_at",
        "sent_at",
    )
    list_filter = ("channel", "category", "status")
    search_fields = ("title", "message", "recipient__email")
    readonly_fields = ("sent_at", "delivered_at", "read_at", "retry_count", "created_at", "updated_at")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
