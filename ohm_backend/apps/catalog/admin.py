"""
apps/catalog/admin.py
─────────────────────
Django admin registrations for catalog models (Venues, Services, Resources, Photos).
"""

from django.contrib import admin, messages
from django.contrib.contenttypes.admin import GenericTabularInline
from django.db import connection
from django_tenants.utils import get_public_schema_name

from .models import Photos, Resource, Service, Venue


class TenantModelAdmin(admin.ModelAdmin):
    """Base ModelAdmin for tenant-scoped models to prevent errors in public schema."""

    def has_module_permission(self, request):
        if connection.schema_name == get_public_schema_name():
            return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if connection.schema_name == get_public_schema_name():
            return False
        return super().has_view_permission(request, obj)


# ---------------------------------------------------------------------------
# Inlines
# ---------------------------------------------------------------------------

class PhotosInline(GenericTabularInline):
    model = Photos
    extra = 1
    fields = ("image", "is_primary", "uploaded_at")
    readonly_fields = ("uploaded_at",)


class ResourceVenueInline(admin.TabularInline):
    model = Resource
    fk_name = "venue"
    extra = 0
    fields = ("name", "total_quantity", "available_quantity", "sell_price_per_unit", "is_active")
    show_change_link = True


class ResourceServiceInline(admin.TabularInline):
    model = Resource
    fk_name = "service"
    extra = 0
    fields = ("name", "total_quantity", "available_quantity", "rent_price_per_unit_per_day", "is_active")
    show_change_link = True


class ServiceVenueInline(admin.TabularInline):
    model = Service.venue.through
    extra = 0
    verbose_name = "Associated Service"
    verbose_name_plural = "Associated Services"


# ---------------------------------------------------------------------------
# ModelAdmins
# ---------------------------------------------------------------------------

@admin.register(Photos)
class PhotosAdmin(TenantModelAdmin):
    list_display = ("id", "content_type", "object_id", "content_object", "is_primary", "uploaded_at")
    list_filter = ("content_type", "is_primary", "uploaded_at")
    ordering = ("-uploaded_at",)
    readonly_fields = ("uploaded_at",)


@admin.register(Venue)
class VenueAdmin(TenantModelAdmin):
    list_display = (
        "id", "name", "owner", "capacity", "price_per_event",
        "rooms", "floors", "is_active", "is_deleted", "created_at",
    )
    list_filter = (
        "is_active", "is_deleted",
        "external_decorators_allow", "external_caterers_allow",
        "created_at",
    )
    search_fields = (
        "name", "description", "contact",
        "location__city", "location__building_name", "owner__email",
    )
    raw_id_fields = ("owner", "location")
    filter_horizontal = ("manager", "staff")
    readonly_fields = ("created_at", "updated_at")
    inlines = [PhotosInline, ServiceVenueInline, ResourceVenueInline]
    ordering = ("-created_at",)
    actions = ("activate_selected", "deactivate_selected", "soft_delete_selected", "restore_selected")

    @admin.action(description="Activate selected venues")
    def activate_selected(self, request, queryset):
        count = queryset.update(is_active=True)
        self.message_user(request, f"{count} venue(s) successfully activated.", messages.SUCCESS)

    @admin.action(description="Deactivate selected venues")
    def deactivate_selected(self, request, queryset):
        count = queryset.update(is_active=False)
        self.message_user(request, f"{count} venue(s) successfully deactivated.", messages.SUCCESS)

    @admin.action(description="Soft delete selected venues")
    def soft_delete_selected(self, request, queryset):
        count = 0
        for venue in queryset:
            venue.soft_delete()
            count += 1
        self.message_user(request, f"{count} venue(s) soft deleted.", messages.SUCCESS)

    @admin.action(description="Restore soft-deleted venues")
    def restore_selected(self, request, queryset):
        count = queryset.update(is_deleted=False, is_active=True)
        self.message_user(request, f"{count} venue(s) restored.", messages.SUCCESS)


@admin.register(Service)
class ServiceAdmin(TenantModelAdmin):
    list_display = (
        "id", "name", "owner", "service_type", "city",
        "contact", "is_active", "is_deleted", "created_at",
    )
    list_filter = ("service_type", "is_active", "is_deleted", "city", "created_at")
    search_fields = ("name", "description", "city", "contact", "address", "owner__email")
    raw_id_fields = ("owner",)
    filter_horizontal = ("manager", "staff", "venue")
    readonly_fields = ("created_at", "updated_at")
    inlines = [PhotosInline, ResourceServiceInline]
    ordering = ("-created_at",)
    actions = ("activate_selected", "deactivate_selected", "soft_delete_selected", "restore_selected")

    @admin.action(description="Activate selected services")
    def activate_selected(self, request, queryset):
        count = queryset.update(is_active=True)
        self.message_user(request, f"{count} service(s) successfully activated.", messages.SUCCESS)

    @admin.action(description="Deactivate selected services")
    def deactivate_selected(self, request, queryset):
        count = queryset.update(is_active=False)
        self.message_user(request, f"{count} service(s) successfully deactivated.", messages.SUCCESS)

    @admin.action(description="Soft delete selected services")
    def soft_delete_selected(self, request, queryset):
        count = 0
        for service in queryset:
            service.soft_delete()
            count += 1
        self.message_user(request, f"{count} service(s) soft deleted.", messages.SUCCESS)

    @admin.action(description="Restore soft-deleted services")
    def restore_selected(self, request, queryset):
        count = queryset.update(is_deleted=False, is_active=True)
        self.message_user(request, f"{count} service(s) restored.", messages.SUCCESS)


@admin.register(Resource)
class ResourceAdmin(TenantModelAdmin):
    list_display = (
        "id", "name", "owner", "venue", "service",
        "total_quantity", "available_quantity",
        "sell_price_per_unit", "rent_price_per_unit_per_day",
        "is_active", "is_deleted",
    )
    list_filter = ("is_active", "is_deleted", "created_at")
    search_fields = ("name", "description", "address", "owner__email")
    raw_id_fields = ("owner", "venue", "service")
    filter_horizontal = ("manager", "staff")
    readonly_fields = ("created_at", "updated_at")
    inlines = [PhotosInline]
    ordering = ("-created_at",)
    actions = ("activate_selected", "deactivate_selected", "soft_delete_selected", "restore_selected")

    @admin.action(description="Activate selected resources")
    def activate_selected(self, request, queryset):
        count = queryset.update(is_active=True)
        self.message_user(request, f"{count} resource(s) successfully activated.", messages.SUCCESS)

    @admin.action(description="Deactivate selected resources")
    def deactivate_selected(self, request, queryset):
        count = queryset.update(is_active=False)
        self.message_user(request, f"{count} resource(s) successfully deactivated.", messages.SUCCESS)

    @admin.action(description="Soft delete selected resources")
    def soft_delete_selected(self, request, queryset):
        count = 0
        for resource in queryset:
            resource.soft_delete()
            count += 1
        self.message_user(request, f"{count} resource(s) soft deleted.", messages.SUCCESS)

    @admin.action(description="Restore soft-deleted resources")
    def restore_selected(self, request, queryset):
        count = queryset.update(is_deleted=False, is_active=True)
        self.message_user(request, f"{count} resource(s) restored.", messages.SUCCESS)
