"""
apps/billing/urls.py
────────────────────
Router-based URL config for the billing app.
Exposes quotes, quote tax settings, invoices, and payments.
Legacy care catalog endpoints for locations, packages, and patient documents
remain available under `/api/care/` via `care_urls.py`.
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

