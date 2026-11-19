"""
apps/platform/admin_base.py
───────────────────────────
Base ModelAdmin classes enforcing schema isolation in the Django admin panel.
"""
from django.contrib import admin
from django.db import connection
from django_tenants.utils import get_public_schema_name


class TenantModelAdmin(admin.ModelAdmin):
    """ModelAdmin for tenant-scoped models.

    Hides tenant models when browsing the public schema to prevent missing-table
    crashes and cross-tenant leakage.
    """

    def has_module_permission(self, request):
        if connection.schema_name == get_public_schema_name():
            return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if connection.schema_name == get_public_schema_name():
            return False
        return super().has_view_permission(request, obj)


class PlatformModelAdmin(admin.ModelAdmin):
    """ModelAdmin for platform/public-scoped models.

    Hides platform models when browsing a tenant schema so tenant administrators
    cannot view or edit control-plane subscriptions, plans, or tenants.
    """

    def has_module_permission(self, request):
        if connection.schema_name != get_public_schema_name():
            return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if connection.schema_name != get_public_schema_name():
            return False
        return super().has_view_permission(request, obj)
