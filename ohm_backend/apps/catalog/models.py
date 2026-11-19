from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.billing.constants import BookingType


class Photos(models.Model):
    image = models.ImageField(upload_to="entity_photos/")
    is_primary = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    class Meta:
        ordering = ["-is_primary", "uploaded_at"]

    def __str__(self):
        return f"Photo {self.id} (Primary={self.is_primary})"


class Venue(models.Model):
    photos = GenericRelation(Photos, related_query_name="venue_photos")
    logo = models.ImageField(upload_to="logo_image/", null=True, blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="own_venues",
    )
    manager = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="managed_venues",
    )
    staff = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="assigned_venues",
        blank=True,
        help_text="Staff assigned to this venue",
    )

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    location = models.OneToOneField(
        "billing.Location",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="venue_location",
    )
    contact = models.CharField(max_length=15, blank=True, null=True)
    website = models.URLField(max_length=500, blank=True, null=True, help_text="Official website")
    social_links = models.JSONField(blank=True, null=True)

    capacity = models.PositiveIntegerField(default=0, help_text="Total pax capacity")
    price_per_event = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    rooms = models.PositiveSmallIntegerField(default=0)
    floors = models.PositiveSmallIntegerField(default=0)
    packages = GenericRelation("billing.Package", related_query_name="venue_packages")
    parking_slots = models.JSONField(default=dict, blank=True, null=True)

    external_decorators_allow = models.BooleanField(default=False)
    external_caterers_allow = models.BooleanField(default=False)

    amenities = models.JSONField(default=list, blank=True, null=True)
    seating_arrangement = models.JSONField(default=list, blank=True, null=True)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def soft_delete(self):
        self.is_active = False
        self.is_deleted = True
        self.save()

    def __str__(self):
        return f"{self.name} ({self.id})"

    class Meta:
        indexes = [
            models.Index(fields=["owner", "is_active"]),
            models.Index(fields=["is_active", "is_deleted"]),
        ]


class Service(models.Model):
    photos = GenericRelation(Photos, related_query_name="service_photos")
    logo = models.ImageField(upload_to="logo_image/", null=True, blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="own_services",
    )
    manager = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="managed_services",
    )
    staff = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="assigned_services",
        blank=True,
        help_text="Staff assigned to this service",
    )
    venue = models.ManyToManyField(Venue, blank=True, related_name="services_of_venue")
    service_type = models.CharField(
        max_length=25,
        choices=BookingType.choices,
        default=BookingType.OPD,
        db_index=True,
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)

    contact = models.CharField(max_length=15, blank=True, null=True)
    website = models.URLField(max_length=500, blank=True, null=True)
    packages = GenericRelation("billing.Package", related_query_name="service_packages")

    tags = models.JSONField(default=list, blank=True)
    quick_info = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def soft_delete(self):
        self.is_active = False
        self.is_deleted = True
        self.save(update_fields=["is_active", "is_deleted"])

    def __str__(self):
        return f"{self.name} ({self.id})"

    class Meta:
        indexes = [
            models.Index(fields=["owner", "is_active"]),
            models.Index(fields=["service_type"]),
        ]


class Resource(models.Model):
    photos = GenericRelation(Photos, related_query_name="resource_photos")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="own_resource",
    )
    manager = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="managed_resources",
    )
    staff = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="assigned_resource",
        blank=True,
        help_text="Staff assigned to this resource",
    )

    name = models.CharField(max_length=200)
    address = models.TextField(blank=True)
    description = models.TextField(blank=True)

    total_quantity = models.PositiveIntegerField(default=1)
    available_quantity = models.PositiveIntegerField(default=1)
    sell_price_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    rent_price_per_unit_per_day = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tags = models.JSONField(default=list, blank=True, null=True)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    venue = models.ForeignKey(Venue, on_delete=models.SET_NULL, null=True, blank=True, related_name="resources_of_venue")
    service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True, blank=True, related_name="resources_of_service")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def soft_delete(self):
        self.is_active = False
        self.is_deleted = True
        self.save(update_fields=["is_active", "is_deleted"])

    def __str__(self):
        return f"{self.name} ({self.owner})"

    class Meta:
        indexes = [
            models.Index(fields=["owner", "is_active"]),
            models.Index(fields=["available_quantity"]),
        ]
