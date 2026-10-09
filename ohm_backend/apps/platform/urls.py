# urls.py
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TenantViewSet, TenantRequestViewSet

router = DefaultRouter()
router.register("tenants", TenantViewSet, basename="tenant")

urlpatterns = [
    path("platform/", include(router.urls)),
    path("tenant-requests/",TenantRequestViewSet.as_view({"get": "list", "post": "create"}),name="tenant-request-list"),
    path("tenant-requests/<uuid:pk>/",TenantRequestViewSet.as_view({"get": "retrieve", "patch": "partial_update"}),name="tenant-request-detail"),
]