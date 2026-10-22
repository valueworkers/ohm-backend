from django.db import connection, transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import AuthenticationFailed,ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Permission, Role, RoleAssignment, User
from apps.accounts.permissions import DynamicModelPermissions, IsTenantAdmin, IsTenantSchema
from apps.accounts.serializers import (
    CustomerRegisterSerializer,
    OTPPasswordResetSerializer,
    OTPRequestSerializer,
    UnifiedLoginSerializer,
    OwnerCreateSerializer,
    OwnerSerializer,
    UserSerializer,
    RoleSerializer,
    RoleAssignmentSerializer,
)

from .helpers import consume_otp, find_user, has_password, issue_otp

# ------------------------ Shared response helpers ------------------------
def _invalid(message="Invalid credentials."):
    return Response({"error": message}, status=status.HTTP_401_UNAUTHORIZED)

# ------------------------ Authentication: login / logout ------------------------
class UnifiedLoginView(APIView):
    """Login with email or phone, using either password or OTP."""

    authentication_classes = []
    permission_classes = [AllowAny]
    # throttle_classes = [ScopedRateThrottle]
    # throttle_scope = "root_login"

    def post(self, request):
        serializer = UnifiedLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = find_user(data["username"])
        if not user:
            return _invalid() if data["method"] == "password" else _invalid("Invalid or expired OTP.")

        # --- Verify credentials (password or OTP) ---
        if data["method"] == "password":
            print(f"has_password:{has_password(user)}")
            if not has_password(user):
                if user.is_active:
                    return Response(
                        {
                            "error": "Password has not been set for this account. Please try OTP login.",
                            "code": "password_not_set",
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                return _invalid()
            if not user.check_password(data["password"]):
                return _invalid()
        else:
            if not consume_otp(user, "login", data["otp"]):
                return _invalid("Invalid or expired OTP.")

        # --- Account state checks (credentials already verified) ---
        if not user.is_active:
            return Response(
                {"error": "Account pending approval by Master Admin."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # --- Issue tokens ---
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        refresh = RefreshToken.for_user(user)
        schema_name = connection.schema_name
        refresh["schema"] = schema_name
        return Response(
            {
                "message": "Login successful!",
                "schema_name": schema_name,
                "user": UserSerializer(user).data,
                "tokens": {"refresh": str(refresh), "access": str(refresh.access_token)},
            },
            status=status.HTTP_200_OK,
        )


class UnifiedLogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data.get("refresh_token")
            if refresh_token:
                RefreshToken(refresh_token).blacklist()
            return Response({"message": "Logout successful"}, status=status.HTTP_200_OK)
        except Exception:
            return Response({"error": "Invalid token or logout failed"}, status=status.HTTP_400_BAD_REQUEST)


class OwnerViewSet(viewsets.ModelViewSet):
    """
    /owners/            GET list, POST add co-owner
    /owners/{id}/       GET, PATCH/PUT, DELETE (soft: deactivates)
    /owners/me/         GET, PATCH own profile
    """

    permission_classes = [IsTenantAdmin]
    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]

    def owners_qs(self):
        return User.objects.filter(
            role_assignments__role__code=Role.Code.TENANT_ADMIN,
            role_assignments__scope_type__isnull=True,
        ).distinct()

    def get_queryset(self):
        return self.owners_qs().order_by("date_joined")

    def get_serializer_class(self):
        return OwnerCreateSerializer if self.action == "create" else OwnerSerializer

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = ser.save()
        return Response(OwnerSerializer(user).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get", "patch"], url_path="me")
    def me(self, request):
        if request.method == "GET":
            return Response(OwnerSerializer(request.user).data)
        ser = OwnerSerializer(request.user, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    def destroy(self, request, *args, **kwargs):
        owner = self.get_object()
        if owner.pk == request.user.pk:
            return Response({"detail": "You cannot remove yourself."}, status=400)
        if not self.owners_qs().filter(is_active=True).exclude(pk=owner.pk).exists():
            return Response({"detail": "Cannot remove the last owner."}, status=400)
        owner.is_active = False
        owner.save(update_fields=["is_active"])
        return Response(status=status.HTTP_204_NO_CONTENT)

# ------------------------ Authentication: OTP request / password reset ------------------------
class OTPRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    # throttle_classes = [ScopedRateThrottle]
    # throttle_scope = "otp_request"

    def post(self, request):
        serializer = OTPRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = find_user(data["username"])
        # Identical response for unknown, inactive and ineligible accounts.
        if user and user.is_active:
            issue_otp(user, data["username"], data["purpose"])
        return Response(
            {"detail": "If the account exists, a verification code has been sent."},
            status=status.HTTP_202_ACCEPTED,
        )


class OTPPasswordResetView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    # throttle_classes = [ScopedRateThrottle]
    # throttle_scope = "otp_request"

    def post(self, request):
        serializer = OTPPasswordResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = find_user(data["username"])
        if not user or not user.is_active:
            raise AuthenticationFailed("Invalid or expired OTP.")
        if not consume_otp(user, "password_reset", data["otp"]):
            raise AuthenticationFailed("Invalid or expired OTP.")

        user.set_password(data["new_password"])
        user.save(update_fields=("password",))
        return Response({"detail": "Password has been reset."})

# ------------------------ Tenant: customer self-registration ------------------------
class TenantRegistrationCreateView(APIView):
    """Let a provider's customers create login accounts in that provider's schema."""

    authentication_classes = []
    permission_classes = [IsTenantSchema]
    # throttle_classes = [ScopedRateThrottle]
    # throttle_scope = "tenant_registration_create"

    def post(self, request):
        serializer = CustomerRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            user = User.objects.create_user(
                email=data["email"],
                password=data["password"],
                full_name=data["full_name"],
                phone=data["phone"],
            )
            RoleAssignment.objects.create(user=user, role=Role.objects.get(code=Role.Code.CUSTOMER))
        return Response({"email": user.email}, status=status.HTTP_201_CREATED)

# ------------------------ Tenant: user management (Tenant Admin only) ------------------------
class UserViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Let a provider's Tenant Admin manage accounts stored in that provider's schema."""

    serializer_class = UserSerializer
    permission_classes = [DynamicModelPermissions]
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
            queryset = queryset.filter(Q(email__icontains=search) | Q(full_name__icontains=search))

        return queryset.distinct()


class RoleViewSet(viewsets.ModelViewSet):
    """Tenant Admin CRUD for workspace roles; core roles remain protected."""

    serializer_class = RoleSerializer
    permission_classes = [IsTenantAdmin]
    queryset = Role.objects.prefetch_related("permissions").order_by("rank", "name")
    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]

    @action(detail=False, methods=["get"])
    def permissions(self, request):
        permissions = Permission.objects.order_by("code")
        return Response([
            {"id": permission.id, "code": permission.code, "name": permission.name,
             "description": permission.description}
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
    permission_classes = [IsTenantAdmin]
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
