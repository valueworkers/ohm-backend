"""
apps/billing/urls.py
────────────────────
Router-based URL config for the billing app.
Exposes quotes, quote tax settings, invoices, and payments.
Clinical patient records, packages, locations, and care booking orders
are handled under the plan-gated Medicare module (`apps.medicare` at `/api/medicare/`).
"""

from rest_framework.routers import DefaultRouter

from .views import (
    InvoiceViewSet,
    PaymentViewSet,
    QuoteTaxSettingsViewSet,
    QuoteViewSet,
)

router = DefaultRouter()
router.register(r"quotes", QuoteViewSet, basename="quote")
router.register(r"quote-tax-settings", QuoteTaxSettingsViewSet, basename="quote-tax-settings")
router.register(r"invoices", InvoiceViewSet, basename="invoice")
router.register(r"payments", PaymentViewSet, basename="payment")

urlpatterns = router.urls

