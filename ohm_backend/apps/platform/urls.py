from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    PlanViewSet,
    SlugAvailabilityView,
    SubscriptionViewSet,
    MyTenantViewSet,
    TenantProvisioningViewSet,
    TenantViewSet,
)

router = DefaultRouter()
router.register("onboarding/requests", TenantProvisioningViewSet, basename="onboarding-request")
router.register("my/tenants", MyTenantViewSet, basename="my-tenant")
router.register("tenants", TenantViewSet, basename="tenant")
router.register("subscriptions", SubscriptionViewSet, basename="subscription")
router.register("plans", PlanViewSet, basename="plan")

urlpatterns = [
    path("onboarding/check-slug/", SlugAvailabilityView.as_view(), name="check-slug"),
    path("", include(router.urls)),
]
