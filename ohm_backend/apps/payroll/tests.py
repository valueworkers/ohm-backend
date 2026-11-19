from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from .models import Attendance, AttendanceStatus, SalaryReport, SalaryStructure, SalaryTransaction
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
