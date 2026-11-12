from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    EntityAssignUsersAPI,
    PhotoViewSet,
    ResourceViewSet,
    ServiceViewSet,
    VenueViewSet,
)

app_name = "catalog"

router = DefaultRouter()
router.register(r"venues", VenueViewSet, basename="venue")
router.register(r"services", ServiceViewSet, basename="service")
router.register(r"resources", ResourceViewSet, basename="resource")
router.register(r"photos", PhotoViewSet, basename="photo")

urlpatterns = [
    path("", include(router.urls)),
    path("assign-users/<str:entity_type>/", EntityAssignUsersAPI.as_view(), name="assign-users"),
]
