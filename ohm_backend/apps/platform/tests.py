from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.platform.models import Tenant, Booking, Quote, Invoice, Payment, Plan

User = get_user_model()


class PlatformTenantEntitiesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            email="superadmin@ohm.com", password="password123"
        )
        self.tenant = Tenant.objects.create(
            name="Sunrise Healthcare",
            schema_name="sunrise",
            contact_email="sunrise@example.com",
            contact_phone="9876543210",
        )
        self.plan = Plan.objects.create(
            name="Professional",
            code="pro",
            amount=Decimal("5000.00"),
            billing_period="monthly",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_booking_crud_and_convert_to_invoice(self):
        now = timezone.now()
        data = {
            "tenant": self.tenant.id,
            "title": "Onboarding & System Setup",
            "booking_type": "onboarding",
            "status": "confirmed",
            "start_datetime": now.isoformat(),
            "end_datetime": (now + timezone.timedelta(hours=2)).isoformat(),
            "total_amount": "2500.00",
            "notes": "Kickoff with IT team",
        }
        res = self.client.post("/api/platform/bookings/", data, format="json")
        self.assertEqual(res.status_code, 201)
        booking_id = res.data["id"]
        self.assertEqual(res.data["tenant_name"], "Sunrise Healthcare")
        self.assertEqual(res.data["contact_name"], "Sunrise Healthcare")

        # List with filter
        list_res = self.client.get(f"/api/platform/bookings/?tenant={self.tenant.id}")
        self.assertEqual(list_res.status_code, 200)
        self.assertEqual(len(list_res.data["results"]), 1)

        # Convert to invoice
        conv_res = self.client.post(f"/api/platform/bookings/{booking_id}/convert-to-invoice/")
        self.assertEqual(conv_res.status_code, 201)
        self.assertEqual(conv_res.data["tenant"], self.tenant.id)
        self.assertEqual(conv_res.data["booking"], booking_id)

    def test_quote_with_tenant(self):
        data = {
            "tenant": self.tenant.id,
            "state": "Karnataka",
            "valid_days": 30,
            "lines": [
                {
                    "item": "Enterprise Setup",
                    "kind": "custom",
                    "qty": "1.00",
                    "uom": "PCS",
                    "unit_price": "10000.00",
                    "gst_rate": "18.00",
                }
            ],
        }
        res = self.client.post("/api/platform/quotes/", data, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["tenant"], self.tenant.id)
        self.assertEqual(res.data["tenant_name"], "Sunrise Healthcare")

        # List with tenant filter
        list_res = self.client.get(f"/api/platform/quotes/?tenant={self.tenant.id}")
        self.assertEqual(list_res.status_code, 200)
        self.assertGreaterEqual(len(list_res.data["results"]), 1)

    def test_payment_crud_with_tenant(self):
        # Create payment directly for tenant
        data = {
            "tenant": self.tenant.id,
            "amount": "5000.00",
            "currency": "INR",
            "method": "bank_transfer",
            "status": "succeeded",
            "reference": "UTR123456789",
        }
        res = self.client.post("/api/platform/payments/", data, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["tenant"], self.tenant.id)
        self.assertEqual(res.data["tenant_name"], "Sunrise Healthcare")

        # List payments filtered by tenant
        list_res = self.client.get(f"/api/platform/payments/?tenant={self.tenant.id}")
        self.assertEqual(list_res.status_code, 200)
        self.assertEqual(len(list_res.data["results"]), 1)
