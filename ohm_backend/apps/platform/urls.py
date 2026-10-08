# urls.py
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TenantViewSet, OnboardingRequestViewSet, RootLoginView

router = DefaultRouter()
router.register("tenants", TenantViewSet, basename="tenant")

urlpatterns = [
    path("api/platform/login/", RootLoginView.as_view(), name="root-login"),
    path("api/platform/", include(router.urls)),
    path(
        "api/onboarding/requests/",
        OnboardingRequestViewSet.as_view({"get": "list", "post": "create"}),
        name="onboarding-request-list",
    ),
    path(
        "api/onboarding/requests/<uuid:pk>/",
        OnboardingRequestViewSet.as_view(
            {"get": "retrieve", "put": "update", "patch": "partial_update"}
        ),
        name="onboarding-request-detail",
    ),
]
