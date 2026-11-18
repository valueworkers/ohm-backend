from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    DashboardKPIAPIView,
    InvoiceListViewSet,
    PaymentMasterViewSet,
    SalaryAnalysisAPIView,
    UserAttendanceAPIView,
)

app_name = "analytics"

router = DefaultRouter()
router.register(r"payment-master", PaymentMasterViewSet, basename="payment-master")
router.register(r"invoices", InvoiceListViewSet, basename="invoice")

urlpatterns = [
    path("salary/", SalaryAnalysisAPIView.as_view(), name="salary-analysis"),
    path("attendance/", UserAttendanceAPIView.as_view(), name="attendance-analysis"),
    path("dashboard-kpis/", DashboardKPIAPIView.as_view(), name="dashboard-kpis"),
    path("", include(router.urls)),
]
