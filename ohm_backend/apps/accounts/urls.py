from django.urls import include, path
from rest_framework.routers import DefaultRouter
from apps.accounts.auth import ScopedTokenRefreshView

from .public_views import (
    ChangePasswordView, OTPPasswordResetView, OTPRequestView, OwnerViewSet,
    RevokeAllSessionsView, ResetPasswordView, UnifiedLoginView, UnifiedLogoutView,
    UserProfileView, UserSessionsView, VerifyOTPView,
)
from .tenant_views import (
    BulkEmployeeUploadAPIView, CustomerViewSet, EmployeeViewSet,
    RoleAssignmentViewSet, RoleViewSet, ShiftScheduleViewSet,
    StaffForHireViewSet, TenantRegistrationCreateView, UserDocumentViewSet, UserViewSet,
)
from .settings_views import (
    AppearanceSettingsView, EmailChangeView, EmailResendView, EmailVerifyView,
    NotificationSettingsView, SettingsProfileView,
)

router = DefaultRouter()
router.register("users", UserViewSet, basename="tenant-user")
router.register("owner", OwnerViewSet, basename="owner")
router.register("roles", RoleViewSet, basename="tenant-role")
router.register("role-assignments", RoleAssignmentViewSet, basename="tenant-role-assignment")
router.register("shifts", ShiftScheduleViewSet, basename="shifts")
router.register("employees", EmployeeViewSet, basename="employees")
router.register("customers", CustomerViewSet, basename="customers")
router.register("user-documents", UserDocumentViewSet, basename="user-documents")
router.register("staff-for-hire", StaffForHireViewSet, basename="staff-for-hire")

urlpatterns = [
    path("", include(router.urls)),
    path("login/", UnifiedLoginView.as_view(), name="login"),
    path("logout/", UnifiedLogoutView.as_view(), name="logout"),
    path("change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("request-otp/", OTPRequestView.as_view(), name="request-otp"),
    path("verify-otp/", VerifyOTPView.as_view(), name="verify-otp"),
    path("reset-password/", ResetPasswordView.as_view(), name="reset-password"),
    path("password-reset/", OTPPasswordResetView.as_view(), name="password-reset"),
    path("register/customer/", TenantRegistrationCreateView.as_view(), name="register-customer"),
    path("token/refresh/", ScopedTokenRefreshView.as_view(), name="token-refresh"),
    path("profile/", UserProfileView.as_view(), name="profile"),
    path("settings/profile/", SettingsProfileView.as_view(), name="settings-profile"),
    path("settings/email/change/", EmailChangeView.as_view(), name="settings-email-change"),
    path("settings/email/verify/", EmailVerifyView.as_view(), name="settings-email-verify"),
    path("settings/email/resend-verification/", EmailResendView.as_view(), name="settings-email-resend"),
    path("settings/appearance/", AppearanceSettingsView.as_view(), name="settings-appearance"),
    path("settings/notifications/", NotificationSettingsView.as_view(), name="settings-notifications"),
    path("settings/security/sessions/", UserSessionsView.as_view(), name="settings-security-sessions"),
    path("settings/security/sessions/revoke-all/", RevokeAllSessionsView.as_view(), name="settings-security-sessions-revoke-all"),
    path("employees-bulk-upload/", BulkEmployeeUploadAPIView.as_view(), name="employees-bulk-upload"),
]
