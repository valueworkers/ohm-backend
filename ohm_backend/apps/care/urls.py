from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AttendanceStatusViewSet,
    AttendanceViewSet,
    CareRazorpayViewSet,
    ContactBookingViewSet,
    LocationViewSet,
    CareRazorpayViewSet,
    PackageViewSet,
    PatientDocumentViewSet,
    PatientViewSet,
    PrimaryOrderViewSet,
    SecondaryOrderViewSet,
    TenantPaymentConfigViewSet,
    TernaryOrderViewSet,
)

router = DefaultRouter()
router.register(r"patients", PatientViewSet, basename="care-patient")
router.register(r"patient-documents", PatientDocumentViewSet, basename="care-patient-document")
router.register(r"locations", LocationViewSet, basename="care-location")
router.register(r"packages", PackageViewSet, basename="care-package")
router.register(r"primary-orders", PrimaryOrderViewSet, basename="care-primary-order")
router.register(r"secondary-orders", SecondaryOrderViewSet, basename="care-secondary-order")
router.register(r"ternary-orders", TernaryOrderViewSet, basename="care-ternary-order")
router.register(r"contact-bookings", ContactBookingViewSet, basename="care-contact-booking")
router.register(r"payment-configs", TenantPaymentConfigViewSet, basename="care-payment-configs")
router.register(r"attendance-status", AttendanceStatusViewSet, basename="care-attendance-status")
router.register(r"attendance-statuses", AttendanceStatusViewSet, basename="care-attendance-statuses")
router.register(r"attendance", AttendanceViewSet, basename="care-attendance")
router.register(r"attendances", AttendanceViewSet, basename="care-attendances")

urlpatterns = [
    # Razorpay payment operations (canonical + alias routes)
    path("razorpay/initiate/", CareRazorpayViewSet.as_view({"post": "initiate"}), name="care-razorpay-initiate"),
    path("razorpay/verify/", CareRazorpayViewSet.as_view({"post": "verify"}), name="care-razorpay-verify"),
    path("payments/razorpay/initiate/", CareRazorpayViewSet.as_view({"post": "initiate"}), name="care-razorpay-initiate-alias"),
    path("payments/razorpay/verify/", CareRazorpayViewSet.as_view({"post": "verify"}), name="care-razorpay-verify-alias"),

    # Singleton TenantPaymentConfig endpoints
    path(
        "payment-config/",
        TenantPaymentConfigViewSet.as_view({
            "get": "list",
            "patch": "partial_update_singleton",
            "put": "update_singleton",
        }),
        name="care-payment-config-singleton",
    ),

    path("", include(router.urls)),
]
