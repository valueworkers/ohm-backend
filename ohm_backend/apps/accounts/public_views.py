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
    OTPPasswordResetSerializer,
    OTPRequestSerializer,
    UnifiedLoginSerializer,
    OwnerCreateSerializer,
    OwnerSerializer,
    MedicareUserSerializer,
)
from apps.accounts.throttles import AuthIdentityThrottle, OTPIdentityThrottle

from .helpers import consume_otp, find_user, has_password, issue_otp

# ------------------------ Shared response helpers ------------------------

"""Platform and cross-schema authentication, identity, and owner views."""

def _invalid(message="Invalid credentials."):
    return Response({"error": message}, status=status.HTTP_401_UNAUTHORIZED)

class UnifiedLoginView(APIView):
    """Login with email or phone, using either password or OTP."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle, AuthIdentityThrottle]
    throttle_scope = "root_login"

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

        schema_name = connection.schema_name
        session = UserSession.objects.create(
            user=user,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
            ip_address=request.META.get("REMOTE_ADDR") or None,
        )
        refresh = RefreshToken.for_user(user)
        refresh["schema"] = schema_name
        refresh["sid"] = str(session.pk)
        session.refresh_jti = refresh["jti"]
        session.save(update_fields=["refresh_jti"])
        return Response(
            {
                "message": "Login successful!",
                "schema_name": schema_name,
                "user": MedicareUserSerializer(user, context={"request": request}).data,
                "tokens": {"refresh": str(refresh), "access": str(refresh.access_token)},
            },
            status=status.HTTP_200_OK,
        )

class UnifiedLogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        session_id = getattr(request.auth, "get", lambda *_: None)("sid")
        if not session_id:
            raise AuthenticationFailed("Session expired. Please sign in again.")
        session = UserSession.objects.filter(
            pk=session_id, user=request.user, revoked_at__isnull=True
        ).first()
        if not session:
            raise AuthenticationFailed("Session has already been revoked.")
        refresh_token = request.data.get("refresh_token")
        if refresh_token:
            try:
                refresh = RefreshToken(refresh_token)
            except Exception:
                raise ValidationError({"refresh_token": "Invalid refresh token."})
            if (refresh.get("schema") != connection.schema_name
                    or refresh.get("sid") != str(session.pk)
                    or str(refresh.get("user_id")) != str(request.user.pk)
                    or refresh.get("jti") != session.refresh_jti):
                raise ValidationError({"refresh_token": "Refresh token does not belong to this active session."})
        session.revoked_at = timezone.now()
        session.save(update_fields=["revoked_at"])
        return Response({"message": "Logout successful"}, status=status.HTTP_200_OK)

class OwnerViewSet(viewsets.ModelViewSet):
    """
    /owners/            GET list, POST add co-owner
    /owners/{id}/       GET, PATCH/PUT, DELETE (soft: deactivates)
    /owners/me/         GET, PATCH own profile
    """

    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]
    permission_classes = [IsSchemaAdministrator]

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

class OTPRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle, OTPIdentityThrottle]
    throttle_scope = "otp_request"

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
    throttle_classes = [ScopedRateThrottle, OTPIdentityThrottle]
    throttle_scope = "otp_request"

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
        UserSession.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())
        return Response({"detail": "Password has been reset."})

class UserProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(MedicareUserSerializer(request.user, context={"request": request}).data)

    def put(self, request):
        if "password" in request.data or "confirm_password" in request.data:
            raise ValidationError({"password": "Use POST /api/change-password/ to change your password."})
        serializer = MedicareUserSerializer(request.user, data=request.data, partial=True, context={"request": request})
        for field in ("email", "is_active", "is_deleted", "created_by", "date_joined"):
            if field in serializer.fields:
                serializer.fields[field].read_only = True
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sensitive_auth"

    def post(self, request):
        current = request.data.get("old_password")
        new = request.data.get("new_password")
        if not isinstance(current, str) or not isinstance(new, str) or not current or not new:
            raise ValidationError({"detail": "old_password and new_password are required."})
        if not request.user.check_password(current):
            raise ValidationError({"old_password": "Current password is incorrect."})
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError
        try:
            validate_password(new, request.user)
        except DjangoValidationError as exc:
            raise ValidationError({"new_password": exc.messages})
        request.user.set_password(new)
        request.user.save(update_fields=["password"])
        UserSession.objects.filter(user=request.user, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        return Response({"message": "Password changed successfully."})

class UserSessionsView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sensitive_auth"

    def get(self, request):
        current_id = getattr(request.auth, "get", lambda *_: None)("sid")
        queryset = UserSession.objects.filter(user=request.user).order_by("-created_at")
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = [{
            "id": str(item.pk), "created_at": item.created_at, "last_seen_at": item.last_seen_at,
            "revoked_at": item.revoked_at, "user_agent": item.user_agent,
            "ip_address": item.ip_address, "current": str(item.pk) == str(current_id),
        } for item in page]
        return paginator.get_paginated_response(data)

class RevokeAllSessionsView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sensitive_auth"

    def post(self, request):
        """Revoke all sessions except this one after a password or OTP step-up."""
        password = request.data.get("current_password")
        otp = request.data.get("otp")
        valid = bool(password and request.user.check_password(password))
        if not valid and otp:
            valid = consume_otp(request.user, "login", otp)
        if not valid:
            raise ValidationError({"detail": "Provide a valid current password or a fresh login OTP."})
        current_id = getattr(request.auth, "get", lambda *_: None)("sid")
        if not current_id:
            raise AuthenticationFailed("Session expired. Please sign in again.")
        updated = UserSession.objects.filter(user=request.user, revoked_at__isnull=True).exclude(
            pk=current_id
        ).update(revoked_at=timezone.now())
        return Response({"revoked_sessions": updated})

class VerifyOTPView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle, OTPIdentityThrottle]
    throttle_scope = "otp_verify"

    def post(self, request):
        username = request.data.get("username")
        otp = request.data.get("otp")
        user = find_user(username or "")
        if not user or not user.is_active or not consume_otp(user, "password_reset", otp or ""):
            raise AuthenticationFailed("Invalid or expired OTP.")
        raw_token = secrets.token_urlsafe(32)
        PasswordResetToken.objects.filter(user=user, consumed_at__isnull=True).update(consumed_at=timezone.now())
        PasswordResetToken.objects.create(
            user=user, token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        return Response({"reset_token": raw_token})

class ResetPasswordView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_verify"

    def post(self, request):
        token = request.data.get("reset_token")
        password = request.data.get("new_password")
        if not isinstance(token, str) or not isinstance(password, str) or not token or not password:
            raise ValidationError({"detail": "reset_token and new_password are required."})
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            from django.contrib.auth.password_validation import validate_password
            from django.core.exceptions import ValidationError as DjangoValidationError
            with transaction.atomic():
                reset = PasswordResetToken.objects.select_for_update().get(
                    token_hash=digest, consumed_at__isnull=True, expires_at__gt=timezone.now(), user__is_active=True
                )
                user = reset.user
                validate_password(password, user)
                user.set_password(password)
                user.save(update_fields=["password"])
                reset.consumed_at = timezone.now()
                reset.save(update_fields=["consumed_at"])
                UserSession.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())
        except PasswordResetToken.DoesNotExist:
            raise AuthenticationFailed("Invalid, expired, or already used password reset token.")
        except DjangoValidationError as exc:
            raise ValidationError({"new_password": exc.messages})
        return Response({"message": "Password reset successful."})
