from django.core.exceptions import PermissionDenied, ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, views, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import is_root_user, is_superadmin, is_tenant_admin
from .filters import ResourceFilter, ServiceFilter, VenueFilter
from .models import Photos, Resource, Service, Venue
from .permissions import CanAssignUsers, can_manage_entity
from .serializers import (
    EntityAssignUsersSerializer,
    PhotoUploadSerializer,
    PhotosSerializer,
    ResourceDropdownSerializer,
    ResourceSerializer,
    ServiceDropdownSerializer,
    ServiceSerializer,
    VenueDropdownSerializer,
    VenueSerializer,
)
from .services import EntityAssignmentService, PhotoService


class VenueViewSet(viewsets.ModelViewSet):
    serializer_class = VenueSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_class = VenueFilter
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "name",
        "description",
        "location__building_name",
        "location__address_line1",
        "location__address_line2",
        "location__locality",
        "location__city",
        "location__state",
    ]
    ordering_fields = ["name", "capacity", "price_per_event", "created_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        user = self.request.user
        qs = Venue.objects.select_related("owner", "location").prefetch_related(
            "manager", "staff", "photos"
        ).filter(is_deleted=False)

        if not user or not user.is_authenticated:
            return qs.filter(is_active=True)

        if is_superadmin(user) or is_root_user(user) or is_tenant_admin(user):
            return qs

        if getattr(user, "is_owner", False):
            return qs.filter(owner=user)
        elif getattr(user, "is_manager", False):
            return qs.filter(manager=user)
        elif user.role_assignments.filter(role__code="STAFF").exists():
            return qs.filter(staff=user)

        return qs.filter(is_active=True)

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_authenticated:
            raise PermissionDenied("Authentication required to create a venue.")
        serializer.save(owner=user, is_active=True)

    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=False, methods=["get"])
    def dropdown(self, request):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = VenueDropdownSerializer(queryset, many=True)
        return Response(serializer.data)


class ServiceViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_class = ServiceFilter
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "description", "address", "city", "contact"]
    ordering_fields = ["name", "created_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        user = self.request.user
        qs = Service.objects.select_related("owner").prefetch_related(
            "manager", "staff", "venue", "photos"
        ).filter(is_deleted=False)

        if not user or not user.is_authenticated:
            return qs.filter(is_active=True)

        if is_superadmin(user) or is_root_user(user) or is_tenant_admin(user):
            return qs

        if getattr(user, "is_owner", False):
            return qs.filter(owner=user)
        elif getattr(user, "is_manager", False):
            return qs.filter(manager=user)
        elif user.role_assignments.filter(role__code="STAFF").exists():
            return qs.filter(staff=user)

        return qs.filter(is_active=True)

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_authenticated:
            raise PermissionDenied("Authentication required to create a service.")
        serializer.save(owner=user, is_active=True)

    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=False, methods=["get"])
    def dropdown(self, request):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = ServiceDropdownSerializer(queryset, many=True)
        return Response(serializer.data)


class ResourceViewSet(viewsets.ModelViewSet):
    serializer_class = ResourceSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_class = ResourceFilter
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "description", "address"]
    ordering_fields = [
        "name",
        "available_quantity",
        "total_quantity",
        "sell_price_per_unit",
        "rent_price_per_unit_per_day",
        "created_at",
    ]
    ordering = ["-created_at"]

    def get_queryset(self):
        user = self.request.user
        qs = Resource.objects.select_related("owner", "venue", "service").prefetch_related(
            "manager", "staff", "photos"
        ).filter(is_deleted=False)

        if not user or not user.is_authenticated:
            return qs.filter(is_active=True)

        if is_superadmin(user) or is_root_user(user) or is_tenant_admin(user):
            return qs

        if getattr(user, "is_owner", False):
            return qs.filter(owner=user)
        elif getattr(user, "is_manager", False):
            return qs.filter(manager=user)
        elif user.role_assignments.filter(role__code="STAFF").exists():
            return qs.filter(staff=user)

        return qs.filter(is_active=True)

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_authenticated:
            raise PermissionDenied("Authentication required to create a resource.")
        serializer.save(owner=user, is_active=True)

    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=False, methods=["get"])
    def dropdown(self, request):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = ResourceDropdownSerializer(queryset, many=True)
        return Response(serializer.data)


class PhotoViewSet(viewsets.GenericViewSet):
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    queryset = Photos.objects.all()
    serializer_class = PhotosSerializer

    @action(detail=False, methods=["post"], url_path="upload")
    def upload(self, request):
        serializer = PhotoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        entity_type = serializer.validated_data["entity_type"]
        object_id = serializer.validated_data["object_id"]
        image = serializer.validated_data["image"]
        is_primary = serializer.validated_data.get("is_primary", False)

        try:
            model_cls, _ = PhotoService.get_model_and_content_type(entity_type)
            entity = model_cls.objects.get(id=object_id)
        except Exception:
            return Response(
                {"detail": f"{entity_type.capitalize()} with id {object_id} not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not can_manage_entity(request.user, entity):
            return Response(
                {"detail": "You do not have permission to attach photos to this entity."},
                status=status.HTTP_403_FORBIDDEN,
            )

        photo, _ = PhotoService.attach_photo(
            entity_type=entity_type,
            object_id=object_id,
            image_file=image,
            is_primary=is_primary,
        )
        return Response(PhotosSerializer(photo, context={"request": request}).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch"], url_path="set-primary")
    def set_primary(self, request, pk=None):
        photo = self.get_object()
        entity = photo.content_object
        if entity and not can_manage_entity(request.user, entity):
            return Response(
                {"detail": "You do not have permission to modify photos for this entity."},
                status=status.HTTP_403_FORBIDDEN,
            )

        updated_photo = PhotoService.set_primary(photo.id)
        return Response(PhotosSerializer(updated_photo, context={"request": request}).data)

    def destroy(self, request, pk=None):
        photo = self.get_object()
        entity = photo.content_object
        if entity and not can_manage_entity(request.user, entity):
            return Response(
                {"detail": "You do not have permission to delete this photo."},
                status=status.HTTP_403_FORBIDDEN,
            )

        PhotoService.delete_photo(photo.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class EntityAssignUsersAPI(views.APIView):
    permission_classes = [IsAuthenticated, CanAssignUsers]

    def post(self, request, entity_type):
        serializer = EntityAssignUsersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        entity_id = serializer.validated_data["entity_id"]
        employee_ids = serializer.validated_data["employee_ids"]

        try:
            entity = EntityAssignmentService.get_entity(entity_type, entity_id)
            if not can_manage_entity(request.user, entity):
                return Response(
                    {"error": f"You do not have permission to assign users to this {entity_type}."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            result = EntityAssignmentService.assign_users(entity_type, entity_id, employee_ids)
            return Response({
                "message": f"Employees assigned successfully to {entity_type}.",
                **result,
            }, status=status.HTTP_200_OK)

        except ValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, entity_type):
        serializer = EntityAssignUsersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        entity_id = serializer.validated_data["entity_id"]
        employee_ids = serializer.validated_data["employee_ids"]

        try:
            entity = EntityAssignmentService.get_entity(entity_type, entity_id)
            if not can_manage_entity(request.user, entity):
                return Response(
                    {"error": f"You do not have permission to unassign users from this {entity_type}."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            result = EntityAssignmentService.unassign_users(entity_type, entity_id, employee_ids)
            return Response({
                "message": f"Employees unassigned successfully from {entity_type}.",
                **result,
            }, status=status.HTTP_200_OK)

        except ValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
