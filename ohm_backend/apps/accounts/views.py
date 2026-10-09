import secrets
import time
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ImproperlyConfigured
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.module_loading import import_string
from rest_framework import mixins, viewsets
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.accounts.auth import TenantTokenObtainPairSerializer
from apps.accounts.models import Role, RoleAssignment, User
from apps.accounts.permissions import IsTenantAdmin
from apps.accounts.serializers import (
    CustomerRegisterSerializer,
    OTPPasswordResetSerializer,
    OTPRequestSerializer,
    UnifiedLoginSerializer,
    UserSerializer,
)


class TenantLoginView(TokenObtainPairView):
    """Log in a tenant user and return tenant-bound JWTs."""

    serializer_class = TenantTokenObtainPairSerializer


def _find_user(username):
    username = username.strip()
    if "@" in username:
        matches = User.objects.filter(email__iexact=username)
    else:
        matches = User.objects.filter(phone=username)
    return matches.first() if matches.count() == 1 else None


def _otp_key(user, purpose):
    # User IDs repeat across schemas, so include the active schema in the key.
    return f"otp:{connection.schema_name}:{user.pk}:{purpose}"


def _send_otp(user, username, code, purpose):
    if "@" in username:
        send_mail(
            subject="Your OHM verification code",
            message=f"Your {purpose.replace('_', ' ')} code is {code}. It expires in 5 minutes.",
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[username],
            fail_silently=False,
        )
        return
    backend_path = getattr(settings, "OHM_SMS_OTP_BACKEND", "")
    if not backend_path:
        raise ImproperlyConfigured("Configure OHM_SMS_OTP_BACKEND to send OTPs to phone numbers.")
    backend = import_string(backend_path)
    backend(phone=user.phone, code=code, purpose=purpose)


class UnifiedLoginView(APIView):
    """Authenticate against public users or the tenant selected by X-Tenant."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "root_login"

    def post(self, request):
        serializer = UnifiedLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = _find_user(data["username"])
        if not user or not user.is_active:
            raise AuthenticationFailed("Invalid credentials.")

        if data["method"] == "password":
            if not user.check_password(data["password"]):
                raise AuthenticationFailed("Invalid credentials.")
        else:
            if not _consume_otp(user, "login", data["otp"]):
                raise AuthenticationFailed("Invalid or expired OTP.")

        if connection.schema_name == "public" and not user.is_superuser and not hasattr(user, "platform_admin_profile"):
            raise AuthenticationFailed("Invalid credentials.")
        refresh = RefreshToken.for_user(user)
        refresh["schema"] = connection.schema_name
        return Response({"refresh": str(refresh), "access": str(refresh.access_token)})


def _consume_otp(user, purpose, code):
    key = _otp_key(user, purpose)
    challenge = cache.get(key)
    if challenge is None or challenge["attempts"] >= 5 or challenge["expires_at"] <= time.time():
        return False
    lock_key = f"{key}:verify-lock"
    if not cache.add(lock_key, "1", timeout=5):
        return False
    try:
        challenge = cache.get(key)
        if challenge is None or challenge["attempts"] >= 5 or challenge["expires_at"] <= time.time():
            return False
        if check_password(code, challenge["code_hash"]):
            cache.delete(key)
            return True
        challenge["attempts"] += 1
        cache.set(key, challenge, timeout=max(1, int(challenge["expires_at"] - time.time())))
        return False
    finally:
        cache.delete(lock_key)


class OTPRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_request"

    def post(self, request):
        serializer = OTPRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = _find_user(data["username"])
        # Keep responses identical for unknown, inactive, and ineligible accounts.
        if user and user.is_active and (connection.schema_name != "public" or user.is_superuser or hasattr(user, "platform_admin_profile")):
            code = f"{secrets.randbelow(1_000_000):06d}"
            key = _otp_key(user, data["purpose"])
            cache.set(key, {"code_hash": make_password(code), "attempts": 0, "expires_at": time.time() + 300}, timeout=300)
            try:
                _send_otp(user, data["username"], code, data["purpose"])
            except Exception:
                cache.delete(key)
                raise
        return Response({"detail": "If the account exists, a verification code has been sent."}, status=202)


class OTPPasswordResetView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_request"

    def post(self, request):
        serializer = OTPPasswordResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = _find_user(data["username"])
        if not user or not user.is_active or (connection.schema_name == "public" and not user.is_superuser and not hasattr(user, "platform_admin_profile")):
            raise AuthenticationFailed("Invalid or expired OTP.")
        if not _consume_otp(user, "password_reset", data["otp"]):
            raise AuthenticationFailed("Invalid or expired OTP.")
        user.set_password(data["new_password"])
        user.save(update_fields=("password",))
        return Response({"detail": "Password has been reset."})


class TenantCurrentSessionView(APIView):
    """Return the current authenticated tenant user's profile and workspace."""

    def get(self, request):
        user = request.user
        tenant = getattr(connection, "tenant", None)
        return Response({
            "email": user.email,
            "full_name": user.full_name,
            "workspace": getattr(tenant, "name", None),
            "modules": getattr(tenant, "modules", []),
            "roles": [
                {
                    "code": assignment.role.code,
                    "scope_type": assignment.scope_type.model if assignment.scope_type else None,
                    "scope_id": assignment.scope_id,
                }
                for assignment in user.role_assignments.select_related("role", "scope_type")
            ],
        })


class TenantRegistrationCreateView(APIView):
    """Let a provider's customers create login accounts in that provider's schema."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "tenant_registration_create"

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
        return Response({"email": user.email}, status=201)


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
            queryset = queryset.filter(role_assignments__role__code=role, role_assignments__scope_type__isnull=True)
        search = self.request.query_params.get("q")
        if search:
            queryset = queryset.filter(Q(email__icontains=search) | Q(full_name__icontains=search))
        return queryset.distinct()
