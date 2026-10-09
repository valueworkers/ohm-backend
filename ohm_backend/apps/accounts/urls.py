from django.urls import include, path
from rest_framework.routers import SimpleRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    OTPPasswordResetView,
    OTPRequestView,
    TenantCurrentSessionView,
    TenantLoginView,
    TenantRegistrationCreateView,
    TenantUserViewSet,
    UnifiedLoginView,
)

router = SimpleRouter()
router.register("users", TenantUserViewSet, basename="tenant-user")

urlpatterns = [
    # Authentication and the signed-in user's own profile.
    path("auth/registrations/", TenantRegistrationCreateView.as_view(), name="tenant-registration-create"),
    path("auth/login/", UnifiedLoginView.as_view(), name="unified-login"),
    path("auth/otp/", OTPRequestView.as_view(), name="otp-request"),
    path("auth/password-reset/", OTPPasswordResetView.as_view(), name="otp-password-reset"),
    path("auth/access-tokens/", TokenRefreshView.as_view(), name="tenant-access-token-create"),
    path("auth/session/", TenantCurrentSessionView.as_view(), name="tenant-current-session"),
    
    # Tenant Admin operations on other accounts in this workspace.
    path("", include(router.urls)),
]
