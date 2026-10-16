from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Role, RoleAssignment, User
from apps.accounts.permissions import IsTenantAdmin
from apps.accounts.serializers import (
    CustomerRegisterSerializer,
    OTPPasswordResetSerializer,
    OTPRequestSerializer,
    UnifiedLoginSerializer,
    UserSerializer,
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
        refresh["schema"] = connection.schema_name
        return Response(
            {
                "message": "Login successful!",
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
    permission_classes = [AllowAny]
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
class TenantUserViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Let a provider's Tenant Admin manage accounts stored in that provider's schema."""

    serializer_class = UserSerializer
    permission_classes = [IsTenantAdmin]
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