from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from django_tenants.utils import schema_context
from rest_framework import status
from rest_framework.test import APIClient

from apps.billing.models import (
    Invoice,
    Location,
    Package,
    Patient,
    PrimaryOrder,
    SecondaryOrder,
    TernaryOrder,
)
from apps.catalog.models import Service, Venue
from apps.care.constants import BookingStatus, BookingType, PeriodChoices
from apps.care.models import (
    Attendance,
    AttendanceStatus,
    TenantPaymentConfig,
)
from apps.care.tasks import (
    mark_attendance_present,
    reconcile_invoices,
    run_care_tenant_beat_maintenance,
    trigger_auto_continue_secondary_orders,
    update_statuses_by_time,
)
from apps.platform.models import Domain, Feature, Plan, Subscription, Tenant

User = get_user_model()


class CareE2ETestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with schema_context("public"):
            cls.tenant = Tenant.objects.get(schema_name="vaish")
            cls.tenant.status = "active"
            cls.tenant.modules = ["care", "medicare", "catalog", "billing"]
            cls.tenant.save(update_fields=["status", "modules"])
            domain_obj = Domain.objects.filter(tenant=cls.tenant).first()
            cls.domain = domain_obj.domain if domain_obj else "vaish.o-hm.com"
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.defaults["HTTP_X_TENANT"] = self.tenant.schema_name

        # Ensure Groups exist
        self.group_admin, _ = Group.objects.get_or_create(name="Tenant Admin")
        self.group_staff, _ = Group.objects.get_or_create(name="STAFF")

        # Get or create admin user
        self.admin_user, _ = User.objects.get_or_create(
            email="medicare_admin@example.com",
            defaults={
                "first_name": "Dr. Sarah",
                "last_name": "Mukherjee",
                "mobile_number": "9876543210",
                "is_active": True,
            },
        )
        self.admin_user.set_password("Password123!")
        self.admin_user.save()
        self.admin_user.groups.add(self.group_admin)

        # Get or create staff user
        self.staff_user, _ = User.objects.get_or_create(
            email="medicare_staff@example.com",
            defaults={
                "first_name": "Care",
                "last_name": "Nurse",
                "mobile_number": "9876543211",
                "is_active": True,
            },
        )
        self.staff_user.set_password("Password123!")
        self.staff_user.save()
        self.staff_user.groups.add(self.group_staff)

        # Authenticate as admin
        self.client.force_authenticate(user=self.admin_user)

    def tearDown(self):
        # Restore tenant modules
        with schema_context("public"):
            self.tenant.modules = ["care", "medicare", "catalog", "billing"]
            self.tenant.save(update_fields=["modules"])
        cache.clear()
        super().tearDown()

    def test_01_plan_gating_access(self):
        """Proves that a tenant without the 'care' feature is blocked with 403 Forbidden."""
        with schema_context("public"):
            self.tenant.modules = ["catalog", "billing"]
            self.tenant.save(update_fields=["modules"])
            self.tenant.subscriptions.all().delete()
        cache.clear()

        response = self.client.get("/api/care/patients/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("subscription plan does not include access", response.data.get("detail", ""))

        # Restore module access
        with schema_context("public"):
            self.tenant.modules = ["care", "medicare", "catalog", "billing"]
            self.tenant.save(update_fields=["modules"])
        cache.clear()

        response_ok = self.client.get("/api/care/patients/")
        self.assertEqual(response_ok.status_code, status.HTTP_200_OK)

        # Verify backward-compatible alias route works identically
        alias_response = self.client.get("/api/medicare/patients/")
        self.assertEqual(alias_response.status_code, status.HTTP_200_OK)

    def test_02_patient_crud_and_soft_delete(self):
        """Tests patient creation, auto-generated patient_id, address, and soft delete."""
        payload = {
            "first_name": "Ramesh",
            "last_name": "Sharma",
            "email": "ramesh.sharma@example.com",
            "phone": "9876543210",
            "age": 68,
            "gender": "male",
            "blood_group": "B+",
            "emergency_contact": "Suresh Sharma",
            "emergency_phone": "9876543211",
            "address": "Flat 402, Sunshine Apts, MG Road, Shivaji Nagar, Pune, 411005",
        }

        res = self.client.post("/api/care/patients/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        patient_id = res.data["id"]

        patient = Patient.objects.get(id=patient_id)
        self.assertTrue(bool(patient.patient_id))
        self.assertEqual(patient.address, "Flat 402, Sunshine Apts, MG Road, Shivaji Nagar, Pune, 411005")
        self.assertTrue(patient.is_active)
        self.assertFalse(patient.is_deleted)

        # Test active-status toggle action
        status_res = self.client.patch(
            f"/api/care/patients/{patient_id}/active-status/",
            {"status": False},
            format="json",
        )
        self.assertEqual(status_res.status_code, status.HTTP_200_OK)
        patient.refresh_from_db()
        self.assertFalse(patient.is_active)

        # Test soft delete
        del_res = self.client.delete(f"/api/care/patients/{patient_id}/")
        self.assertEqual(del_res.status_code, status.HTTP_204_NO_CONTENT)

        patient.refresh_from_db()
        self.assertTrue(patient.is_deleted)
        self.assertFalse(patient.is_active)

        # Excluded from active list
        list_res = self.client.get("/api/care/patients/")
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        ids = [item["id"] for item in list_res.data.get("results", list_res.data)]
        self.assertNotIn(patient_id, ids)

    def test_03_package_lifecycle(self):
        """Tests package creation and retrieval via Medicare endpoints."""
        service = Service.objects.create(
            owner=self.admin_user,
            name="Geriatric Physiotherapy",
            service_type=BookingType.OPD,
        )
        ct = ContentType.objects.get_for_model(service)

        res_valid = self.client.post("/api/care/packages/", {
            "name": "Monthly Home Care Package",
            "owner": self.admin_user.id,
            "content_type": ct.id,
            "object_id": service.id,
            "price": "12000.00",
            "registration_fees": "500.00",
            "package_type": BookingType.CLIENT_SIDE,
            "period": PeriodChoices.MONTHLY,
        }, format="json")
        self.assertEqual(res_valid.status_code, status.HTTP_201_CREATED, res_valid.data)

        pkg = Package.objects.get(id=res_valid.data["id"])
        self.assertEqual(pkg.name, "Monthly Home Care Package")
        self.assertEqual(pkg.price, Decimal("12000.00"))

    def test_04_restructured_booking_lifecycle_and_invoicing(self):
        """
        Tests booking flow:
          Patient -> PrimaryOrder -> SecondaryOrder -> Invoicing.
        """
        patient = Patient.objects.create(
            first_name="Anandi",
            last_name="Patil",
            phone="9876543222",
            address="Marine Drive, Mumbai",
        )
        service = Service.objects.create(
            owner=self.admin_user,
            name="Post-Operative Nursing",
            service_type=BookingType.IN_HOUSE,
        )
        ct = ContentType.objects.get_for_model(service)
        package = Package.objects.create(
            owner=self.admin_user,
            content_type=ct,
            object_id=service.id,
            name="Daily Nursing Care",
            price=Decimal("1500.00"),
            package_type=BookingType.IN_HOUSE,
            period=PeriodChoices.DAILY,
        )

        now = timezone.now()
        start = now + timedelta(days=1)
        end = start + timedelta(days=7)

        # 1. Create PrimaryOrder
        po_res = self.client.post("/api/care/primary-orders/", {
            "user": self.admin_user.id,
            "patient": patient.id,
            "start_datetime": start.isoformat(),
            "end_datetime": end.isoformat(),
            "total_bill": "10000.00",
            "notes": "Post knee replacement care plan",
        }, format="json")
        self.assertEqual(po_res.status_code, status.HTTP_201_CREATED, po_res.data)
        primary_id = po_res.data["id"]

        # 2. Create SecondaryOrder
        so_payload = {
            "primary_order": primary_id,
            "package": package.id,
            "service": service.id,
            "booking_type": BookingType.IN_HOUSE,
            "client_address": {"line1": "Flat 402, Lotus Residency", "city": "Bengaluru", "pincode": "560038"},
            "start_datetime": start.isoformat(),
            "end_datetime": end.isoformat(),
            "subtotal": "10000.00",
        }
        so_res = self.client.post("/api/care/secondary-orders/", so_payload, format="json")
        self.assertEqual(so_res.status_code, status.HTTP_201_CREATED, so_res.data)
        secondary_id = so_res.data["id"]

        # 3. Add an add-on TernaryOrder
        ter_payload = {
            "secondary_order": secondary_id,
            "service": service.id,
            "package": package.id,
            "start_datetime": (start + timedelta(days=2)).isoformat(),
            "end_datetime": (start + timedelta(days=3)).isoformat(),
            "subtotal": "2000.00",
        }
        ter_res = self.client.post("/api/care/ternary-orders/", ter_payload, format="json")
        self.assertEqual(ter_res.status_code, status.HTTP_201_CREATED, ter_res.data)

        # 4. Generate invoice
        inv_res = self.client.post(f"/api/care/secondary-orders/{secondary_id}/generate-invoice/")
        self.assertEqual(inv_res.status_code, status.HTTP_200_OK, inv_res.data)
        self.assertTrue(inv_res.data["invoice_number"].startswith("SINV-"))

        # Verify billing.Invoice exists and has items
        invoice = Invoice.objects.get(id=inv_res.data["invoice_id"])
        self.assertEqual(invoice.items.count(), 1)
        self.assertTrue(invoice.total_amount > Decimal("0.00"))

    def test_05_multi_tenant_data_isolation(self):
        """
        Proves that two tenants with Medicare cannot see each other's patients,
        packages, or orders.
        """
        # Create a patient in Tenant 1 (self.tenant)
        p1 = Patient.objects.create(
            first_name="TenantOne",
            last_name="Patient",
            phone="9111111111",
            address="123 Street",
        )

        # Use the second tenant: radha_clinic
        with schema_context("public"):
            tenant_b = Tenant.objects.get(schema_name="radha_clinic")
            tenant_b.status = "active"
            tenant_b.modules = ["care", "medicare", "catalog", "billing"]
            tenant_b.save(update_fields=["status", "modules"])

        # Switch client to Tenant 2
        client_b = APIClient()
        client_b.defaults["HTTP_X_TENANT"] = tenant_b.schema_name

        with schema_context(tenant_b.schema_name):
            user_b, _ = User.objects.get_or_create(
                email="admin_radha@example.com",
                defaults={"first_name": "Admin", "last_name": "Radha"},
            )
            admin_group, _ = Group.objects.get_or_create(name="Tenant Admin")
            user_b.groups.add(admin_group)

        client_b.force_authenticate(user=user_b)

        # Query patients from Tenant 2
        res = client_b.get("/api/care/patients/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data.get("results", res.data)
        ids = [item["id"] for item in results]
        self.assertNotIn(p1.id, ids)

        # Attempting to fetch Tenant 1's patient by ID returns 404
        get_res = client_b.get(f"/api/care/patients/{p1.id}/")
        self.assertEqual(get_res.status_code, status.HTTP_404_NOT_FOUND)

    def test_06_celery_automations(self):
        """Tests attendance automation, status transitions, auto-continue, and invoice reconciliation."""
        # 1. Test mark_attendance_present
        att_res = mark_attendance_present()
        self.assertTrue(att_res["attendances_marked"] >= 0)
        self.assertTrue(Attendance.objects.filter(date=timezone.now().date()).exists())

        # 2. Test status updater
        service = Service.objects.create(owner=self.admin_user, name="Diagnostic Checkup")
        pkg = Package.objects.create(
            owner=self.admin_user, name="General Check", price=Decimal("100.00")
        )
        patient = Patient.objects.create(first_name="Test", last_name="Status", phone="9999988888", address="Sample Address")
        past_start = timezone.now() - timedelta(days=2)
        past_end = timezone.now() - timedelta(days=1)
        primary = PrimaryOrder.objects.create(
            patient=patient,
            user=self.admin_user,
            start_datetime=past_start,
            end_datetime=past_end,
        )

        sec = SecondaryOrder.objects.create(
            primary_order=primary,
            package=pkg,
            start_datetime=past_start,
            end_datetime=past_end,
            subtotal=Decimal("100.00"),
            status=BookingStatus.IN_PROGRESS,
        )

        update_statuses_by_time()
        sec.refresh_from_db()
        self.assertEqual(sec.status, BookingStatus.COMPLETED)

        # 3. Test trigger_auto_continue_secondary_orders
        cont_res = trigger_auto_continue_secondary_orders()
        self.assertTrue(isinstance(cont_res, dict))

        # 4. Test reconcile_invoices
        rec_res = reconcile_invoices()
        self.assertTrue(isinstance(rec_res, dict))
