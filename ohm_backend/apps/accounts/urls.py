from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    BulkEmployeeUploadAPIView, ChangePasswordView, CustomerViewSet, EmployeeViewSet,
    OTPPasswordResetView, OTPRequestView, OwnerViewSet, ResetPasswordView,
    RoleAssignmentViewSet, RoleViewSet, ShiftScheduleViewSet,
    StaffForHireViewSet, UnifiedLoginView, UnifiedLogoutView, UserDocumentViewSet,
    UserProfileView, UserViewSet, VerifyOTPView, TenantRegistrationCreateView,
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
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("profile/", UserProfileView.as_view(), name="profile"),
    path("employees-bulk-upload/", BulkEmployeeUploadAPIView.as_view(), name="employees-bulk-upload"),
]
