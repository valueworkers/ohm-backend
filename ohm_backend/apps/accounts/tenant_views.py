from django.db import connection, transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404
import hashlib
import secrets
from datetime import timedelta

from django.utils import timezone
from rest_framework.decorators import action
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import AuthenticationFailed,ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from config.pagination import StandardResultsSetPagination

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from django.contrib.auth.models import Group, Permission
from apps.accounts.models import (
    EmployeeProfile, ShiftSchedule, StaffForHire,
    User, UserDocument, UserDocumentFile, UserSession,
)
from apps.accounts.permissions import CanManageRoles, IsTenantAdmin, IsTenantSchema
from apps.accounts.serializers import (
    CustomerRegisterSerializer,
    BulkEmployeeUploadSerializer,
    OTPPasswordResetSerializer,
    OTPRequestSerializer,
    UnifiedLoginSerializer,
    OwnerCreateSerializer,
    OwnerSerializer,
    UserSerializer,
    GroupSerializer,
    PermissionSerializer,
    CustomerListSerializer,
    EmployeeTerminationSerializer,
    MedicareEmployeeSerializer,
    MedicareUserSerializer,
    ShiftScheduleSerializer,
    StaffForHireSerializer,
    UserDocumentSerializer,
)
from apps.accounts.throttles import AuthIdentityThrottle, OTPIdentityThrottle

from .helpers import consume_otp, find_user, has_password, issue_otp

# ------------------------ Shared response helpers ------------------------

"""Tenant workspace registration and administration views."""

class TenantRegistrationCreateView(APIView):
    """Let a provider's customers create login accounts in that provider's schema."""

    authentication_classes = []
    permission_classes = [AllowAny, IsTenantSchema]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "tenant_registration_create"

    def post(self, request):
        serializer = CustomerRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            user = User.objects.create_user(**data)
            customer_group = Group.objects.filter(name__in=["CUSTOMER", "Customer"]).first()
            if customer_group:
                user.groups.add(customer_group)
        return Response({"email": user.email}, status=status.HTTP_201_CREATED)

class UserViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Let a provider's Tenant Admin manage accounts stored in that provider's schema."""

    serializer_class = UserSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        queryset = User.objects.all().order_by("id").prefetch_related(
            "groups", "user_permissions"
        )

        group = self.request.query_params.get("group") or self.request.query_params.get("role")
        if group:
            if str(group).isdigit():
                queryset = queryset.filter(groups__id=group)
            else:
                queryset = queryset.filter(groups__name__iexact=group)

        search = self.request.query_params.get("q")
        if search:
            queryset = queryset.filter(
                Q(email__icontains=search) | Q(first_name__icontains=search)
                | Q(middle_name__icontains=search) | Q(last_name__icontains=search)
            )

        return queryset.distinct()

class GroupViewSet(viewsets.ModelViewSet):
    """CRUD for Django permission groups (used as roles); managed by Superadmin, Root user, and Tenant Admin."""

    serializer_class = GroupSerializer
    permission_classes = [CanManageRoles]
    queryset = Group.objects.prefetch_related("permissions__content_type", "user_set").order_by("name")
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name"]
    ordering_fields = ["name", "id"]
    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]

    def destroy(self, request, *args, **kwargs):
        group = self.get_object()
        if group.name in {"Super User", "Tenant Admin"}:
            raise ValidationError({"name": "Built-in permission groups cannot be deleted."})
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["get", "post", "delete"], url_path="users")
    def users(self, request, pk=None):
        """List, add, or remove users in this permission group / role."""
        group = self.get_object()
        if request.method == "GET":
            users = group.user_set.all().order_by("id")
            return Response([
                {
                    "id": u.id,
                    "email": u.email,
                    "first_name": u.first_name,
                    "last_name": u.last_name,
                    "is_active": u.is_active,
                }
                for u in users
            ])

        user_id = request.data.get("user_id")
        user_ids = request.data.get("user_ids")
        if not user_id and not user_ids and request.method == "DELETE":
            user_id = request.query_params.get("user_id")

        target_ids = list(user_ids) if user_ids else ([user_id] if user_id else [])
        if not target_ids:
            raise ValidationError({"user_id": "Provide user_id or user_ids."})

        target_users = list(User.objects.filter(pk__in=target_ids))
        if not target_users:
            raise ValidationError({"user_id": "No matching active users found."})

        if request.method == "POST":
            for u in target_users:
                u.groups.add(group)
            return Response({
                "message": f"Added {len(target_users)} user(s) to group '{group.name}'.",
                "group_id": group.id,
                "users": [u.id for u in target_users],
            }, status=status.HTTP_200_OK)

        if request.method == "DELETE":
            if group.name in {"Tenant Admin", "TENANT_ADMIN"}:
                for u in target_users:
                    if u.is_active:
                        other_admins = User.objects.filter(
                            is_active=True, groups__name__in=["Tenant Admin", "TENANT_ADMIN"]
                        ).exclude(pk=u.pk).exists()
                        if not other_admins:
                            raise ValidationError({"detail": "Cannot remove the last active Tenant Admin from the group."})
            for u in target_users:
                u.groups.remove(group)
            return Response(status=status.HTTP_204_NO_CONTENT)


class PermissionViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only view for available Django permissions."""

    serializer_class = PermissionSerializer
    permission_classes = [CanManageRoles]
    queryset = Permission.objects.select_related("content_type").order_by("content_type__app_label", "codename")
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["content_type__app_label", "content_type__model"]
    search_fields = ["name", "codename", "content_type__app_label", "content_type__model"]
    ordering_fields = ["codename", "name", "id"]
    http_method_names = ["get", "head", "options"]

    @action(detail=False, methods=["get"], url_path="grouped")
    def grouped(self, request):
        """Return permissions grouped by app_label and model, mirroring Django Admin structure."""
        permissions = self.filter_queryset(self.get_queryset())
        grouped_data = {}
        for perm in permissions:
            app_label = perm.content_type.app_label
            model_name = perm.content_type.model
            if app_label not in grouped_data:
                grouped_data[app_label] = {}
            if model_name not in grouped_data[app_label]:
                grouped_data[app_label][model_name] = []
            grouped_data[app_label][model_name].append({
                "id": perm.id,
                "name": perm.name,
                "codename": perm.codename,
            })
        return Response(grouped_data)


class ShiftScheduleViewSet(viewsets.ModelViewSet):
    queryset = ShiftSchedule.objects.all()
    serializer_class = ShiftScheduleSerializer
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

class EmployeeViewSet(viewsets.ModelViewSet):
    serializer_class = MedicareEmployeeSerializer
    search_fields = ["first_name", "middle_name", "last_name", "email", "mobile_number", "employee_profile__employee_id"]
    ordering_fields = ["id", "first_name", "last_name", "date_joined"]
    ordering = ["id"]

    def get_queryset(self):
        queryset = User.objects.employees().select_related("employee_profile", "employee_profile__shift")
        if self.action == "list":
            term = self.request.query_params.get("q")
            if term:
                queryset = queryset.filter(Q(email__icontains=term) | Q(first_name__icontains=term) | Q(last_name__icontains=term) | Q(employee_profile__employee_id__icontains=term))
            active = self.request.query_params.get("is_active")
            if active is not None:
                queryset = queryset.filter(is_active=active.lower() == "true")
            category = self.request.query_params.get("category")
            if category:
                queryset = queryset.filter(employee_profile__category=category)
            termination = self.request.query_params.get("termination_type")
            if termination:
                queryset = queryset.filter(employee_profile__termination_type=termination)
        return queryset.order_by(*self.ordering)

    def destroy(self, request, *args, **kwargs):
        employee = self.get_object()
        profile = get_object_or_404(EmployeeProfile, user=employee)
        serializer = EmployeeTerminationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            profile.terminate(data["termination_type"], data.get("reason"), data.get("last_working_day"))
        except Exception as exc:
            if hasattr(exc, "message_dict"):
                raise ValidationError(exc.message_dict)
            raise
        return Response({"message": "Employee terminated."})

    @action(detail=True, methods=["post"], url_path="terminate")
    def terminate_employee(self, request, pk=None):
        employee = self.get_object()
        profile = get_object_or_404(EmployeeProfile, user=employee)
        serializer = EmployeeTerminationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            profile.terminate(data["termination_type"], data.get("reason"), data.get("last_working_day"))
        except Exception as exc:
            if hasattr(exc, "message_dict"):
                raise ValidationError(exc.message_dict)
            raise
        return Response(MedicareEmployeeSerializer(employee, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"], url_path="termination-revoke")
    def termination_revoke(self, request, pk=None):
        employee = self.get_object()
        profile = get_object_or_404(EmployeeProfile, user=employee)
        raw = request.data.get("rehired", request.data.get("rehired_status", False))
        rehired = raw is True or str(raw).lower() in ("true", "1", "yes")
        try:
            profile.termination_revoke(rehired)
        except Exception as exc:
            if hasattr(exc, "message_dict"):
                raise ValidationError(exc.message_dict)
            raise
        return Response(MedicareEmployeeSerializer(employee, context=self.get_serializer_context()).data)

class CustomerViewSet(viewsets.ModelViewSet):
    search_fields = ["first_name", "middle_name", "last_name", "email", "mobile_number", "city"]
    ordering = ["first_name"]

    def get_queryset(self):
        return User.objects.customers().filter(is_deleted=False).order_by("-id")

    def get_serializer_class(self):
        return CustomerListSerializer if self.action == "list" else MedicareUserSerializer

    def perform_create(self, serializer):
        user = serializer.save()
        customer_group = Group.objects.filter(name__in=["CUSTOMER", "Customer"]).first()
        if customer_group:
            user.groups.add(customer_group)

    def destroy(self, request, *args, **kwargs):
        user = self.get_object()
        user.soft_delete()
        return Response({"message": "Customer deleted successfully."})

class UserDocumentViewSet(viewsets.ModelViewSet):
    serializer_class = UserDocumentSerializer
    parser_classes = [MultiPartParser, FormParser]
    queryset = UserDocument.objects.select_related("user", "uploaded_by").prefetch_related("files")

    def perform_create(self, serializer):
        document = serializer.save(uploaded_by=self.request.user)
        files = self.request.FILES.getlist("files")
        UserDocumentFile.objects.bulk_create([UserDocumentFile(document=document, file=f) for f in files])

    def perform_update(self, serializer):
        document = serializer.save()
        files = self.request.FILES.getlist("files")
        UserDocumentFile.objects.bulk_create([UserDocumentFile(document=document, file=f) for f in files])

class StaffForHireViewSet(viewsets.ModelViewSet):
    serializer_class = StaffForHireSerializer
    queryset = StaffForHire.objects.select_related("created_by")
    search_fields = ["staff_name", "vendor_name", "available_for", "language", "skill"]
    ordering = ["-created_at"]

    def get_queryset(self):
        queryset = super().get_queryset()
        service = self.request.query_params.get("available_for")
        if service:
            queryset = queryset.filter(available_for__contains=[service])
        if not self.request.user.is_owner:
            queryset = queryset.filter(is_active=True, is_available=True)
        return queryset

class BulkEmployeeUploadAPIView(APIView):
    permission_classes = [IsAuthenticated, IsTenantAdmin]
    parser_classes = [MultiPartParser, FormParser]
    MAX_ROWS = 1000

    def post(self, request):
        serializer = BulkEmployeeUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            from openpyxl import load_workbook
            workbook = load_workbook(serializer.validated_data["file"], read_only=True, data_only=True)
        except Exception as exc:
            raise ValidationError({"file": f"Could not read workbook: {exc}"})
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        headers = [str(value or "").strip().lower().replace(" ", "_") for value in next(rows, ())]
        if not headers or "email" not in headers:
            raise ValidationError({"file": "The first row must contain an email column."})
        results = {"created": [], "failed": []}
        for row_number, row in enumerate(rows, start=2):
            if row_number > self.MAX_ROWS + 1:
                raise ValidationError({"file": f"A maximum of {self.MAX_ROWS} employees can be uploaded at once."})
            record = {header: value for header, value in zip(headers, row) if header}
            if not any(value is not None for value in record.values()):
                continue
            email = str(record.get("email") or "").strip().lower()
            try:
                if not email:
                    raise ValueError("email is required")
                with transaction.atomic():
                    if User.objects.filter(email__iexact=email).exists():
                        raise ValueError("email already exists")
                    profile_fields = {key: record[key] for key in (
                        "employee_id", "category", "designation", "grade", "cost_center", "department",
                        "vendor_name", "vendor_phone", "pf_number", "uan_number", "esi_number", "esi_dispensary",
                    ) if key in record and record[key] is not None}
                    first = str(record.get("first_name") or "")
                    last = str(record.get("last_name") or "")
                    user = User.objects.create_user(
                        email=email, password=None, first_name=first, last_name=last,
                        mobile_number=str(record.get("mobile_number") or "") or None,
                        created_by=request.user,
                    )
                    EmployeeProfile.objects.create(user=user, **profile_fields)
                    code = str(record.get("role") or "STAFF").upper()
                    allowed_roles = {"STAFF", "OPS_MANAGER", "VENUE_ADMIN", "SERVICE_ADMIN", "RESOURCE_ADMIN"}
                    if code not in allowed_roles:
                        raise ValueError("role must be a staff or manager role")
                    from apps.accounts.services import AccountsService
                    group = Group.objects.filter(name__in=[code, AccountsService._group_name(code)]).first()
                    if group:
                        user.groups.add(group)
                results["created"].append({"row": row_number, "email": email, "id": user.pk})
            except Exception as exc:
                results["failed"].append({"row": row_number, "email": email, "error": str(exc)})
        workbook.close()
        results["summary"] = {"created": len(results["created"]), "failed": len(results["failed"])}
        return Response(results, status=status.HTTP_207_MULTI_STATUS if results["failed"] else status.HTTP_200_OK)
