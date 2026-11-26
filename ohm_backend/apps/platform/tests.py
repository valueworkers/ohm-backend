from decimal import Decimal
import json
from django.contrib import admin
from django.db import ProgrammingError, connection
from django.test import RequestFactory, TestCase
from unittest.mock import patch
from django.utils import timezone
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.platform.middleware import TenantHeaderMiddleware

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


class MultiTenantAdminTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.middleware = TenantHeaderMiddleware(lambda req: None)
        self.public_tenant, _ = Tenant.objects.get_or_create(
            schema_name="public",
            defaults={"name": "Public", "status": "active"}
        )
        self.active_tenant, _ = Tenant.objects.get_or_create(
            schema_name="apollo_clinic",
            defaults={"name": "Apollo Clinic", "status": "active"}
        )

    def tearDown(self):
        connection.set_schema_to_public()

    def test_admin_login_get_defaults_to_public(self):
        req = self.rf.get("/admin/login/")
        self.middleware.process_request(req)
        self.assertEqual(connection.schema_name, "public")
        self.assertIsNone(getattr(req, "_admin_tenant_error", None))

    def test_missing_public_tenant_table_returns_service_unavailable(self):
        missing_relation = Exception("undefined table")
        missing_relation.sqlstate = "42P01"
        db_error = ProgrammingError("relation \"platform_tenant\" does not exist")
        db_error.__cause__ = missing_relation

        req = self.rf.get("/api/platform/tenants/")
        with patch.object(Tenant.objects, "get", side_effect=db_error):
            response = self.middleware.process_request(req)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["code"], "public_schema_unavailable")

    def test_admin_login_post_invalid_slug_sets_error_without_400_json(self):
        req = self.rf.post("/admin/login/", {"x_tenant": "invalid!slug"})
        resp = self.middleware.process_request(req)
        # Should not return a raw 400 JsonResponse for admin
        self.assertIsNone(resp)
        self.assertIn("Invalid workspace slug", getattr(req, "_admin_tenant_error", ""))
        self.assertEqual(connection.schema_name, "public")

    def test_admin_login_post_nonexistent_tenant_sets_error_without_404_json(self):
        req = self.rf.post("/admin/login/", {"x_tenant": "nonexistent-clinic"})
        resp = self.middleware.process_request(req)
        self.assertIsNone(resp)
        self.assertIn("Workspace 'nonexistent-clinic' not found", getattr(req, "_admin_tenant_error", ""))
        self.assertEqual(connection.schema_name, "public")

    def test_admin_login_post_valid_tenant_switches_schema(self):
        req = self.rf.post("/admin/login/", {"x_tenant": "apollo-clinic"})
        resp = self.middleware.process_request(req)
        self.assertIsNone(resp)
        self.assertIsNone(getattr(req, "_admin_tenant_error", None))
        self.assertEqual(connection.schema_name, "apollo_clinic")
        self.assertEqual(req.tenant.pk, self.active_tenant.pk)

    def test_admin_get_with_cookie_switches_schema(self):
        req = self.rf.get("/admin/billing/invoice/")
        req.COOKIES["x_tenant"] = "apollo-clinic"
        self.middleware.process_request(req)
        self.assertEqual(connection.schema_name, "apollo_clinic")

    def test_admin_process_response_sets_cookie_for_authenticated_tenant_user(self):
        from django.http import HttpResponse
        req = self.rf.get("/admin/")
        req.user = User(email="staff@apollo.com", is_active=True, is_staff=True)
        req.tenant = self.active_tenant
        response = HttpResponse()
        self.middleware.process_response(req, response)
        self.assertIn("x_tenant", response.cookies)
        self.assertEqual(response.cookies["x_tenant"].value, "apollo_clinic")

    def test_admin_process_response_deletes_cookie_on_logout(self):
        from django.http import HttpResponse
        req = self.rf.post("/admin/logout/")
        response = HttpResponse()
        self.middleware.process_response(req, response)
        self.assertIn("x_tenant", response.cookies)
        self.assertEqual(response.cookies["x_tenant"].value, "")

    def test_tenant_admin_form_prepopulates_from_cookie(self):
        from apps.platform.forms import TenantAdminAuthenticationForm
        req = self.rf.get("/admin/login/")
        req.COOKIES["x_tenant"] = "apollo-clinic"
        form = TenantAdminAuthenticationForm(request=req)
        self.assertEqual(form.fields["x_tenant"].initial, "apollo-clinic")

    def test_tenant_admin_form_rejects_non_staff_user(self):
        from apps.platform.forms import TenantAdminAuthenticationForm
        from django.core.exceptions import ValidationError
        form = TenantAdminAuthenticationForm()
        non_staff = User(email="patient@example.com", is_active=True, is_staff=False)
        with self.assertRaises(ValidationError) as ctx:
            form.confirm_login_allowed(non_staff)
        self.assertIn("does not have staff permissions", str(ctx.exception))

    def test_schema_isolation_permissions(self):
        from apps.platform.admin_base import PlatformModelAdmin, TenantModelAdmin
        class DummyRequest:
            user = User(is_superuser=True, is_active=True)

        req = DummyRequest()
        platform_admin = PlatformModelAdmin(Tenant, admin.site)
        tenant_admin = TenantModelAdmin(Tenant, admin.site)

        # Public schema
        connection.set_schema_to_public()
        self.assertTrue(platform_admin.has_module_permission(req))
        self.assertFalse(tenant_admin.has_module_permission(req))

        # Tenant schema
        connection.set_tenant(self.active_tenant)
        self.assertFalse(platform_admin.has_module_permission(req))
        self.assertTrue(tenant_admin.has_module_permission(req))

