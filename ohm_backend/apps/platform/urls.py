from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    PlanViewSet,
    SlugAvailabilityView,
    SubscriptionViewSet,
    MyTenantViewSet,
    TenantProvisioningViewSet,
    TenantViewSet,
    QuoteStatsView, QuoteViewSet, QuoteTaxSettingsView, PublicQuoteResponseView,
)

router = DefaultRouter()
router.register("onboarding/requests", TenantProvisioningViewSet, basename="onboarding-request")
router.register("my/tenants", MyTenantViewSet, basename="my-tenant")
router.register("tenants", TenantViewSet, basename="tenant")
router.register("subscriptions", SubscriptionViewSet, basename="subscription")
router.register("plans", PlanViewSet, basename="plan")
router.register("quotes", QuoteViewSet, basename="quote")

urlpatterns = [
    path("quotes/stats/", QuoteStatsView.as_view(), name="quote-stats"),
    path("quotes/tax-settings/", QuoteTaxSettingsView.as_view(), name="quote-tax-settings"),
    path("public/quotes/<str:token>/respond/", PublicQuoteResponseView.as_view(), name="public-quote-response"),
    path("onboarding/check-slug/", SlugAvailabilityView.as_view(), name="check-slug"),
    path("", include(router.urls)),
]
