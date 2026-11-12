from django.contrib import admin
from django.contrib.contenttypes.admin import GenericTabularInline

from .models import Photos, Resource, Service, Venue


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


@admin.register(Photos)
class PhotosAdmin(admin.ModelAdmin):
    list_display = ("id", "content_type", "object_id", "is_primary", "uploaded_at")
    list_filter = ("content_type", "is_primary", "uploaded_at")
    ordering = ("-uploaded_at",)


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "owner", "capacity", "price_per_event", "is_active", "is_deleted", "created_at")
    list_filter = ("is_active", "is_deleted", "external_decorators_allow", "external_caterers_allow")
    search_fields = ("name", "description", "location__city", "location__building_name")
    filter_horizontal = ("manager", "staff")
    inlines = [PhotosInline, ServiceVenueInline, ResourceVenueInline]
    ordering = ("-created_at",)


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "owner", "service_type", "city", "is_active", "is_deleted", "created_at")
    list_filter = ("service_type", "is_active", "is_deleted")
    search_fields = ("name", "description", "city", "contact")
    filter_horizontal = ("manager", "staff", "venue")
    inlines = [PhotosInline, ResourceServiceInline]
    ordering = ("-created_at",)


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "owner",
        "venue",
        "service",
        "total_quantity",
        "available_quantity",
        "sell_price_per_unit",
        "rent_price_per_unit_per_day",
        "is_active",
        "is_deleted",
    )
    list_filter = ("is_active", "is_deleted")
    search_fields = ("name", "description")
    filter_horizontal = ("manager", "staff")
    inlines = [PhotosInline]
    ordering = ("-created_at",)
