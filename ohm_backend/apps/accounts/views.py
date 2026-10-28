"""Compatibility exports for split account API views.

Import from ``public_views`` or ``tenant_views`` in new URL configurations.
"""

from .public_views import (
    ChangePasswordView, OTPPasswordResetView, OTPRequestView, OwnerViewSet,
    RevokeAllSessionsView, ResetPasswordView, UnifiedLoginView, UnifiedLogoutView,
    UserProfileView, UserSessionsView, VerifyOTPView,
)
from .tenant_views import (
    BulkEmployeeUploadAPIView, CustomerViewSet, EmployeeViewSet, RoleAssignmentViewSet,
    RoleViewSet, ShiftScheduleViewSet, StaffForHireViewSet, TenantRegistrationCreateView,
    UserDocumentViewSet, UserViewSet,
)
