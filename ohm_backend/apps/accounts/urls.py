from django.urls import include, path
from rest_framework.routers import SimpleRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import TenantCurrentSessionView, TenantLoginView, TenantRegistrationCreateView, TenantUserViewSet

router = SimpleRouter()
router.register("users", TenantUserViewSet, basename="tenant-user")

urlpatterns = [
    # Authentication and the signed-in user's own profile.
    path("auth/registrations/", TenantRegistrationCreateView.as_view(), name="tenant-registration-create"),
    path("auth/login/", TenantLoginView.as_view(), name="tenant-login"),
    path("auth/access-tokens/", TokenRefreshView.as_view(), name="tenant-access-token-create"),
    path("auth/session/", TenantCurrentSessionView.as_view(), name="tenant-current-session"),
    
    # Tenant Admin operations on other accounts in this workspace.
    path("", include(router.urls)),
]
