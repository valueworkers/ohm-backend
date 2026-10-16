from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .views import TenantUserViewSet

router = SimpleRouter()
router.register("users", TenantUserViewSet, basename="tenant-user")

urlpatterns = [
    # Tenant Admin operations on other accounts in this workspace.
    path("", include(router.urls)),
]
