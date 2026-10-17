from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .views import UserViewSet,OwnerViewSet

router = SimpleRouter()
router.register("users", UserViewSet, basename="tenant-user")
router.register("owner", OwnerViewSet, basename="tenant-user")


urlpatterns = [
    # Tenant Admin operations on other accounts in this workspace.
    path("", include(router.urls)),
]
