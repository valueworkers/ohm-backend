from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from .models import Attendance, AttendanceStatus, SalaryReport, SalaryStructure, SalaryTransaction
from .views import AttendanceView, SalaryStructureViewSet
from .utils import PayrollCalculator

User = get_user_model()


class PayrollTests(TenantTestCase):
    @classmethod
    def setup_tenant(cls, tenant):
        tenant.name = "Payroll Provider"
        tenant.status = "active"

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email="emp1@test.com", password="pwd")
        self.status_p = AttendanceStatus.objects.create(code="PRESENT", label="Present")
        self.status_a = AttendanceStatus.objects.create(code="ABSENT", label="Absent")
        self.factory = APIRequestFactory()

    def test_payroll_calculator(self):
        SalaryStructure.objects.create(
            user=self.user,
            salary_type="MONTHLY",
            change_type="BASE_SALARY",
            amount=Decimal("30000.00"),
            effective_from=date(2026, 1, 1),
        )

        Attendance.objects.create(
            user=self.user,
            date=date(2026, 1, 2),
            status=self.status_p,
        )

        calc = PayrollCalculator(self.user)
        reports = calc.reports(start_date=date(2026, 1, 1), end_date=date(2026, 1, 31))
        self.assertTrue(len(reports) >= 1)
        first = reports[0]
        self.assertEqual(first["attendance"]["present_days"], 1)
        self.assertEqual(first["salary"]["daily_rate"], Decimal("1000.00"))

    def test_non_admin_cannot_modify_payroll_records(self):
        structure = SalaryStructure.objects.create(
            user=self.user,
            salary_type="MONTHLY",
            change_type="BASE_SALARY",
            amount=Decimal("30000.00"),
            effective_from=date(2026, 1, 1),
        )

        attendance_request = self.factory.post(
            "/api/payroll/attendance/",
            {"user": self.user.id, "date": "2026-01-02", "status": self.status_p.id},
            format="json",
        )
        force_authenticate(attendance_request, user=self.user)
        self.assertEqual(AttendanceView.as_view()(attendance_request).status_code, 403)

        structure_request = self.factory.delete(f"/api/payroll/salary-structures/{structure.id}/")
        force_authenticate(structure_request, user=self.user)
        response = SalaryStructureViewSet.as_view({"delete": "destroy"})(
            structure_request, pk=structure.id
        )
        self.assertEqual(response.status_code, 403)

    def test_employee_payroll_serializer_uses_mobile_number(self):
        """The dashboard's legacy phone_number key must serialize correctly."""
        from .serializers import EmployeePayrollListSerializer

        self.user.mobile_number = "9876543210"
        self.user.save(update_fields=["mobile_number"])
        self.assertEqual(EmployeePayrollListSerializer(self.user).data["phone_number"], "9876543210")
