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

from django.contrib.auth.models import Permission
from apps.accounts.models import (
    EmployeeProfile, Role, RoleAssignment, ShiftSchedule, StaffForHire,
    PasswordResetToken, User, UserDocument, UserDocumentFile, UserSession,
)
from apps.accounts.permissions import IsSchemaAdministrator, IsTenantAdmin, IsTenantSchema
from apps.accounts.serializers import (
    CustomerRegisterSerializer,
    BulkEmployeeUploadSerializer,
    OTPPasswordResetSerializer,
    OTPRequestSerializer,
    UnifiedLoginSerializer,
    OwnerCreateSerializer,
    OwnerSerializer,
    UserSerializer,
    RoleSerializer,
    RoleAssignmentSerializer,
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
            user = User.objects.create_user(
                **data,
            )
            RoleAssignment.objects.create(user=user, role=Role.objects.get(code=Role.Code.CUSTOMER))
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
    permission_classes = [IsSchemaAdministrator]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        queryset = User.objects.all().order_by("id").prefetch_related("role_assignments__role")

        role = self.request.query_params.get("role")
        if role:
            queryset = queryset.filter(
                role_assignments__role__code=role,
                role_assignments__scope_type__isnull=True,
            )

        search = self.request.query_params.get("q")
        if search:
            queryset = queryset.filter(
                Q(email__icontains=search) | Q(first_name__icontains=search)
                | Q(middle_name__icontains=search) | Q(last_name__icontains=search)
            )

        return queryset.distinct()

class RoleViewSet(viewsets.ModelViewSet):
    """Tenant Admin CRUD for workspace roles; core roles remain protected."""

    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated, IsTenantAdmin]
    queryset = Role.objects.prefetch_related("permissions").order_by("rank", "name")
    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]

    @action(detail=False, methods=["get"])
    def permissions(self, request):
        permissions = Permission.objects.order_by("codename")
        return Response([
            {"id": permission.id, "code": permission.codename, "name": permission.name}
            for permission in permissions
        ])

    def destroy(self, request, *args, **kwargs):
        role = self.get_object()
        if role.code in {Role.Code.TENANT_ADMIN, Role.Code.CUSTOMER}:
            raise ValidationError({"code": "Built-in roles cannot be deleted."})
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "This role is assigned to users. Remove its assignments before deleting it."},
                status=status.HTTP_409_CONFLICT,
            )

class RoleAssignmentViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Tenant Admin role grants, optionally scoped to an object in this tenant."""

    serializer_class = RoleAssignmentSerializer
    permission_classes = [IsAuthenticated, IsTenantAdmin]
    queryset = RoleAssignment.objects.select_related("user", "role", "scope_type").order_by("id")
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        queryset = super().get_queryset()
        user_id = self.request.query_params.get("user_id")
        role_id = self.request.query_params.get("role_id")
        if user_id:
            queryset = queryset.filter(user_id=user_id)
        if role_id:
            queryset = queryset.filter(role_id=role_id)
        return queryset

    def perform_destroy(self, instance):
        if (instance.scope_type_id is None and instance.role.code == Role.Code.TENANT_ADMIN
                and instance.user.is_active):
            another_admin = User.objects.filter(
                is_active=True,
                role_assignments__role__code=Role.Code.TENANT_ADMIN,
                role_assignments__scope_type__isnull=True,
            ).exclude(pk=instance.user_id).exists()
            if not another_admin:
                raise ValidationError({"detail": "A workspace must keep at least one active Tenant Admin."})
        instance.delete()

class ShiftScheduleViewSet(viewsets.ModelViewSet):
    queryset = ShiftSchedule.objects.all()
    serializer_class = ShiftScheduleSerializer
    permission_classes = [IsAuthenticated, IsTenantAdmin]
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

class EmployeeViewSet(viewsets.ModelViewSet):
    serializer_class = MedicareEmployeeSerializer
    permission_classes = [IsAuthenticated, IsTenantAdmin]
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
    permission_classes = [IsAuthenticated, IsTenantAdmin]
    search_fields = ["first_name", "middle_name", "last_name", "email", "mobile_number", "city"]
    ordering = ["first_name"]

    def get_queryset(self):
        return User.objects.customers().filter(is_deleted=False).order_by("-id")

    def get_serializer_class(self):
        return CustomerListSerializer if self.action == "list" else MedicareUserSerializer

    def perform_create(self, serializer):
        user = serializer.save()
        role = Role.objects.filter(code=Role.Code.CUSTOMER).first()
        if role:
            RoleAssignment.objects.get_or_create(user=user, role=role)

    def destroy(self, request, *args, **kwargs):
        user = self.get_object()
        user.soft_delete()
        return Response({"message": "Customer deleted successfully."})

class UserDocumentViewSet(viewsets.ModelViewSet):
    serializer_class = UserDocumentSerializer
    permission_classes = [IsAuthenticated, IsTenantAdmin]
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
    permission_classes = [IsAuthenticated]
    queryset = StaffForHire.objects.select_related("created_by")
    search_fields = ["staff_name", "vendor_name", "available_for", "language", "skill"]
    ordering = ["-created_at"]

    def get_permissions(self):
        if self.action in {"list", "retrieve"}:
            return [IsTenantSchema(), IsAuthenticated()]
        return [IsTenantSchema(), IsTenantAdmin()]

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
                    code = str(record.get("role") or Role.Code.STAFF).upper()
                    allowed_roles = {Role.Code.STAFF, Role.Code.OPS_MANAGER, Role.Code.VENUE_ADMIN, Role.Code.SERVICE_ADMIN, Role.Code.RESOURCE_ADMIN}
                    if code not in allowed_roles:
                        raise ValueError("role must be a staff or manager role")
                    role = Role.objects.get(code=code)
                    RoleAssignment.objects.get_or_create(user=user, role=role)
                results["created"].append({"row": row_number, "email": email, "id": user.pk})
            except Exception as exc:
                results["failed"].append({"row": row_number, "email": email, "error": str(exc)})
        workbook.close()
        results["summary"] = {"created": len(results["created"]), "failed": len(results["failed"])}
        return Response(results, status=status.HTTP_207_MULTI_STATUS if results["failed"] else status.HTTP_200_OK)
