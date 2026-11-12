from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework import serializers

from apps.billing.models import Location
from .constants import ALLOWED_PHOTO_EXTENSIONS, MAX_PHOTO_SIZE_BYTES, EntityType
from .models import Photos, Resource, Service, Venue

User = get_user_model()


class PhotosSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Photos
        fields = [
            "id",
            "image",
            "image_url",
            "is_primary",
            "uploaded_at",
            "content_type",
            "object_id",
        ]
        read_only_fields = ["id", "uploaded_at"]

    def get_image_url(self, obj):
        if obj.image:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url
        return None


class UserMiniSerializer(serializers.ModelSerializer):
    primary_role = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "first_name", "last_name", "email", "mobile_number", "primary_role"]


class LocationMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = [
            "id",
            "building_name",
            "address_line1",
            "address_line2",
            "locality",
            "city",
            "state",
            "postal_code",
            "location_type",
        ]


class VenueSerializer(serializers.ModelSerializer):
    photos = PhotosSerializer(many=True, read_only=True)
    owner = UserMiniSerializer(read_only=True)
    manager = UserMiniSerializer(many=True, read_only=True)
    staff = UserMiniSerializer(many=True, read_only=True)
    location = LocationMiniSerializer(read_only=True)

    # Flat write-only fields for location creation/update
    location_type = serializers.CharField(write_only=True, required=False, default="OPD")
    building_name = serializers.CharField(write_only=True, required=False, default="")
    address_line1 = serializers.CharField(write_only=True, required=False, default="")
    address_line2 = serializers.CharField(write_only=True, required=False, allow_blank=True, default="")
    locality = serializers.CharField(write_only=True, required=False, allow_blank=True, default="")
    city = serializers.CharField(write_only=True, required=False, default="")
    state = serializers.CharField(write_only=True, required=False, default="")
    postal_code = serializers.CharField(write_only=True, required=False, default="")

    class Meta:
        model = Venue
        fields = [
            "id",
            "owner",
            "manager",
            "staff",
            "name",
            "description",
            "contact",
            "website",
            "social_links",
            "capacity",
            "price_per_event",
            "rooms",
            "floors",
            "parking_slots",
            "external_decorators_allow",
            "external_caterers_allow",
            "amenities",
            "seating_arrangement",
            "is_active",
            "is_deleted",
            "created_at",
            "updated_at",
            "photos",
            "logo",
            "location",
            "location_type",
            "building_name",
            "address_line1",
            "address_line2",
            "locality",
            "city",
            "state",
            "postal_code",
        ]
        read_only_fields = ["id", "is_deleted", "created_at", "updated_at"]

    @transaction.atomic
    def create(self, validated_data):
        request = self.context.get("request")
        photos_data = request.FILES.getlist("photos") if request else []
        logo = request.FILES.get("logo") if request else None

        has_location_data = any(
            k in validated_data
            for k in ["building_name", "address_line1", "city", "state", "postal_code"]
        )

        location = None
        if has_location_data:
            location_dict = {
                "building_name": validated_data.pop("building_name", "") or "",
                "address_line1": validated_data.pop("address_line1", "") or "",
                "address_line2": validated_data.pop("address_line2", "") or "",
                "locality": validated_data.pop("locality", "") or "",
                "city": validated_data.pop("city", "") or "",
                "state": validated_data.pop("state", "") or "",
                "postal_code": validated_data.pop("postal_code", "") or "",
                "location_type": validated_data.pop("location_type", "OPD") or "OPD",
                "user": request.user if request and request.user.is_authenticated else None,
            }
            location = Location.objects.create(**location_dict)
        else:
            for k in [
                "building_name", "address_line1", "address_line2",
                "locality", "city", "state", "postal_code", "location_type"
            ]:
                validated_data.pop(k, None)

        if logo:
            validated_data["logo"] = logo

        venue = Venue.objects.create(location=location, **validated_data)

        if photos_data:
            ct = ContentType.objects.get_for_model(Venue)
            Photos.objects.bulk_create([
                Photos(
                    image=img,
                    is_primary=(idx == 0),
                    content_type=ct,
                    object_id=venue.id,
                )
                for idx, img in enumerate(photos_data)
            ])

        return venue

    @transaction.atomic
    def update(self, instance, validated_data):
        request = self.context.get("request")
        photos_data = request.FILES.getlist("photos") if request else []
        logo = request.FILES.get("logo") if request else None

        loc_keys = [
            "building_name", "address_line1", "address_line2",
            "locality", "city", "state", "postal_code", "location_type"
        ]
        has_loc_update = any(k in validated_data for k in loc_keys)

        if has_loc_update:
            if instance.location is None:
                instance.location = Location.objects.create(
                    building_name=validated_data.pop("building_name", "") or "",
                    address_line1=validated_data.pop("address_line1", "") or "",
                    address_line2=validated_data.pop("address_line2", "") or "",
                    locality=validated_data.pop("locality", "") or "",
                    city=validated_data.pop("city", "") or "",
                    state=validated_data.pop("state", "") or "",
                    postal_code=validated_data.pop("postal_code", "") or "",
                    location_type=validated_data.pop("location_type", "OPD") or "OPD",
                    user=request.user if request and request.user.is_authenticated else None,
                )
            else:
                for k in loc_keys:
                    if k in validated_data:
                        setattr(instance.location, k, validated_data.pop(k))
                instance.location.save()
        else:
            for k in loc_keys:
                validated_data.pop(k, None)

        if logo:
            instance.logo = logo

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if photos_data:
            ct = ContentType.objects.get_for_model(Venue)
            Photos.objects.bulk_create([
                Photos(
                    image=img,
                    is_primary=False,
                    content_type=ct,
                    object_id=instance.id,
                )
                for img in photos_data
            ])

        return instance


class VenueDropdownSerializer(serializers.ModelSerializer):
    locality = serializers.CharField(read_only=True, source="location.locality")
    city = serializers.CharField(read_only=True, source="location.city")

    class Meta:
        model = Venue
        fields = ["id", "name", "locality", "city", "price_per_event", "capacity"]


class ServiceSerializer(serializers.ModelSerializer):
    photos = PhotosSerializer(many=True, read_only=True)
    owner = UserMiniSerializer(read_only=True)
    manager = UserMiniSerializer(many=True, read_only=True)
    staff = UserMiniSerializer(many=True, read_only=True)

    class Meta:
        model = Service
        fields = [
            "id",
            "owner",
            "manager",
            "staff",
            "venue",
            "name",
            "service_type",
            "description",
            "address",
            "city",
            "contact",
            "website",
            "tags",
            "quick_info",
            "is_active",
            "is_deleted",
            "created_at",
            "updated_at",
            "photos",
            "logo",
        ]
        read_only_fields = ["id", "is_deleted", "created_at", "updated_at"]

    @transaction.atomic
    def create(self, validated_data):
        request = self.context.get("request")
        photos_data = request.FILES.getlist("photos") if request else []
        logo = request.FILES.get("logo") if request else None

        if logo:
            validated_data["logo"] = logo

        venues = validated_data.pop("venue", None)
        service = Service.objects.create(**validated_data)

        if venues:
            service.venue.set(venues)

        if photos_data:
            ct = ContentType.objects.get_for_model(Service)
            Photos.objects.bulk_create([
                Photos(
                    image=img,
                    is_primary=(idx == 0),
                    content_type=ct,
                    object_id=service.id,
                )
                for idx, img in enumerate(photos_data)
            ])

        return service

    @transaction.atomic
    def update(self, instance, validated_data):
        request = self.context.get("request")
        photos_data = request.FILES.getlist("photos") if request else []
        logo = request.FILES.get("logo") if request else None

        if logo:
            instance.logo = logo

        venues = validated_data.pop("venue", None)
        instance = super().update(instance, validated_data)

        if venues is not None:
            instance.venue.set(venues)

        if photos_data:
            ct = ContentType.objects.get_for_model(Service)
            Photos.objects.bulk_create([
                Photos(
                    image=img,
                    is_primary=False,
                    content_type=ct,
                    object_id=instance.id,
                )
                for img in photos_data
            ])

        return instance


class ServiceDropdownSerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        fields = ["id", "name", "service_type", "city"]


class ResourceSerializer(serializers.ModelSerializer):
    photos = PhotosSerializer(many=True, read_only=True)
    owner = UserMiniSerializer(read_only=True)
    manager = UserMiniSerializer(many=True, read_only=True)
    staff = UserMiniSerializer(many=True, read_only=True)
    venue_name = serializers.CharField(read_only=True, source="venue.name")
    service_name = serializers.CharField(read_only=True, source="service.name")

    class Meta:
        model = Resource
        fields = [
            "id",
            "owner",
            "manager",
            "staff",
            "name",
            "address",
            "description",
            "total_quantity",
            "available_quantity",
            "sell_price_per_unit",
            "rent_price_per_unit_per_day",
            "tags",
            "is_active",
            "is_deleted",
            "venue",
            "venue_name",
            "service",
            "service_name",
            "created_at",
            "updated_at",
            "photos",
        ]
        read_only_fields = ["id", "is_deleted", "created_at", "updated_at"]

    def validate(self, attrs):
        total = attrs.get("total_quantity", getattr(self.instance, "total_quantity", 1))
        avail = attrs.get("available_quantity", getattr(self.instance, "available_quantity", 1))
        if avail > total:
            raise serializers.ValidationError({
                "available_quantity": "Available quantity cannot exceed total quantity."
            })
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        request = self.context.get("request")
        photos_data = request.FILES.getlist("photos") if request else []

        resource = Resource.objects.create(**validated_data)

        if photos_data:
            ct = ContentType.objects.get_for_model(Resource)
            Photos.objects.bulk_create([
                Photos(
                    image=img,
                    is_primary=(idx == 0),
                    content_type=ct,
                    object_id=resource.id,
                )
                for idx, img in enumerate(photos_data)
            ])

        return resource

    @transaction.atomic
    def update(self, instance, validated_data):
        request = self.context.get("request")
        photos_data = request.FILES.getlist("photos") if request else []

        instance = super().update(instance, validated_data)

        if photos_data:
            ct = ContentType.objects.get_for_model(Resource)
            Photos.objects.bulk_create([
                Photos(
                    image=img,
                    is_primary=False,
                    content_type=ct,
                    object_id=instance.id,
                )
                for img in photos_data
            ])

        return instance


class ResourceDropdownSerializer(serializers.ModelSerializer):
    class Meta:
        model = Resource
        fields = [
            "id",
            "name",
            "available_quantity",
            "sell_price_per_unit",
            "rent_price_per_unit_per_day",
        ]


class PhotoUploadSerializer(serializers.Serializer):
    entity_type = serializers.ChoiceField(choices=EntityType.choices)
    object_id = serializers.IntegerField(min_value=1)
    image = serializers.ImageField(required=True)
    is_primary = serializers.BooleanField(default=False)

    def validate_image(self, file_obj):
        ext = file_obj.name.split(".")[-1].lower()
        if ext not in ALLOWED_PHOTO_EXTENSIONS:
            raise serializers.ValidationError(
                f"Unsupported image extension '{ext}'. Allowed: {', '.join(sorted(ALLOWED_PHOTO_EXTENSIONS))}."
            )
        if file_obj.size > MAX_PHOTO_SIZE_BYTES:
            raise serializers.ValidationError("Image file size exceeds the 10 MB limit.")
        return file_obj


class EntityAssignUsersSerializer(serializers.Serializer):
    entity_id = serializers.IntegerField(min_value=1)
    employee_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )
