from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .views import UserViewSet, OwnerViewSet, RoleViewSet, RoleAssignmentViewSet

router = SimpleRouter()
router.register("users", UserViewSet, basename="tenant-user")
router.register("owner", OwnerViewSet, basename="owner")
router.register("roles", RoleViewSet, basename="tenant-role")
router.register("role-assignments", RoleAssignmentViewSet, basename="tenant-role-assignment")


urlpatterns = [
    # Tenant Admin operations on other accounts in this workspace.
    path("", include(router.urls)),
]
