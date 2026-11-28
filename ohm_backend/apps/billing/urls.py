"""
apps/billing/urls.py
────────────────────
Router-based URL config for the billing app.
Exposes billing and care resources under the billing app namespace.
"""

from rest_framework.routers import DefaultRouter

app_name = "billing"

from .views import (
    InvoiceViewSet,
    LocationViewSet,
    PackageViewSet,
    PatientDocumentViewSet,
    PatientViewSet,
    PaymentViewSet,
    QuoteTaxSettingsViewSet,
    QuoteViewSet,
)

router = DefaultRouter()
router.register(r"quotes", QuoteViewSet, basename="quote")
router.register(r"quote-tax-settings", QuoteTaxSettingsViewSet, basename="quote-tax-settings")
router.register(r"invoices", InvoiceViewSet, basename="invoice")
router.register(r"payments", PaymentViewSet, basename="payment")
router.register(r"locations", LocationViewSet, basename="location")
router.register(r"packages", PackageViewSet, basename="package")
router.register(r"patient-documents", PatientDocumentViewSet, basename="patient-document")
router.register(r"patients", PatientViewSet, basename="patient")

urlpatterns = router.urls

