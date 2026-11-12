"""
apps/billing/urls.py
────────────────────
Router-based URL config for the billing app.
Include this in the project root URLconf or the tenant URLconf:

    path("api/billing/", include("apps.billing.urls")),
"""

from rest_framework.routers import DefaultRouter

from .views import (
    ContactBookingViewSet,
    InvoiceViewSet,
    LocationViewSet,
    PackageViewSet,
    PatientDocumentViewSet,
    PatientViewSet,
    PaymentViewSet,
    PrimaryOrderViewSet,
    QuoteTaxSettingsViewSet,
    QuoteViewSet,
    SecondaryOrderViewSet,
    TernaryOrderViewSet,
)

router = DefaultRouter()
router.register(r"locations", LocationViewSet, basename="location")
router.register(r"packages", PackageViewSet, basename="package")
router.register(r"patients", PatientViewSet, basename="patient")
router.register(r"patient-documents", PatientDocumentViewSet, basename="patient-document")
router.register(r"contact-bookings", ContactBookingViewSet, basename="contact-booking")
router.register(r"bookings", ContactBookingViewSet, basename="booking")
router.register(r"primary-orders", PrimaryOrderViewSet, basename="primary-order")
router.register(r"secondary-orders", SecondaryOrderViewSet, basename="secondary-order")
router.register(r"ternary-orders", TernaryOrderViewSet, basename="ternary-order")
router.register(r"quotes", QuoteViewSet, basename="quote")
router.register(r"quote-tax-settings", QuoteTaxSettingsViewSet, basename="quote-tax-settings")
router.register(r"invoices", InvoiceViewSet, basename="invoice")
router.register(r"payments", PaymentViewSet, basename="payment")

urlpatterns = router.urls
