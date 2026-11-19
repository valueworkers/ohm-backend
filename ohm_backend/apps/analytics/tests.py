from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient


class AnalyticsSmokeTests(TenantTestCase):
    @classmethod
    def setup_tenant(cls, tenant):
        tenant.name = "Analytics Provider"
        tenant.status = "active"

    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_analytics_import(self):
        from .views import DashboardKPIAPIView, PaymentMasterViewSet
        self.assertIsNotNone(DashboardKPIAPIView)
        self.assertIsNotNone(PaymentMasterViewSet)
