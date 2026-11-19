from rest_framework import serializers

from apps.billing.models import Invoice


class SalaryPeriodSerializer(serializers.Serializer):
    """One pay period row for an employee."""

    start_date = serializers.DateField()
    end_date = serializers.DateField()
    calendar_days = serializers.IntegerField()
    total_payable_days = serializers.DecimalField(max_digits=10, decimal_places=2)
    total_unpaid_days = serializers.DecimalField(max_digits=10, decimal_places=2)
    attendance_pct = serializers.DecimalField(max_digits=10, decimal_places=2)
    salary = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_salary = serializers.DecimalField(max_digits=12, decimal_places=2)
    payment_status = serializers.CharField(allow_null=True)
    amount_paid = serializers.DecimalField(max_digits=12, decimal_places=2)
    excess_balance = serializers.DecimalField(max_digits=12, decimal_places=2)


class EmployeeSalaryAnalysisSerializer(serializers.Serializer):
    """One employee with all their salary periods nested."""

    id = serializers.IntegerField()
    emp_id = serializers.CharField(allow_null=True)
    first_name = serializers.CharField()
    middle_name = serializers.CharField(allow_null=True, required=False)
    last_name = serializers.CharField(allow_null=True, required=False)
    mobile_number = serializers.CharField(allow_null=True, required=False)
    status = serializers.BooleanField()
    periods = SalaryPeriodSerializer(many=True)


class AttendanceDaySerializer(serializers.Serializer):
    def to_representation(self, instance):
        return instance


class UserAttendanceSerializer(serializers.Serializer):
    user = serializers.IntegerField()
    attendance = serializers.SerializerMethodField()

    def get_attendance(self, obj):
        return obj.get("attendance", {})


class MonthlyAnalyticsRowSerializer(serializers.Serializer):
    month = serializers.CharField()
    invoice_value = serializers.FloatField()
    amt_collected = serializers.FloatField()
    balance = serializers.FloatField()
    collection_pct = serializers.FloatField(allow_null=True)
    paid_invoices = serializers.IntegerField()
    partially_paid_invoices = serializers.IntegerField()
    unpaid_invoices = serializers.IntegerField()
    generated_invoices = serializers.IntegerField()
    unmapped_count = serializers.IntegerField()
    unmapped_amount = serializers.FloatField()
    bookings_starting = serializers.IntegerField()
    bookings_ending = serializers.IntegerField()


class MonthlyAnalyticsSummarySerializer(serializers.Serializer):
    total_invoice_value = serializers.FloatField()
    total_amt_collected = serializers.FloatField()
    total_balance = serializers.FloatField()
    collection_pct = serializers.FloatField(allow_null=True)
    total_paid_invoices = serializers.IntegerField()
    total_partially_paid_invoices = serializers.IntegerField()
    total_unpaid_invoices = serializers.IntegerField()
    total_generated_invoices = serializers.IntegerField()
    total_unmapped_count = serializers.IntegerField()
    total_unmapped_amount = serializers.FloatField()
    total_bookings_starting = serializers.IntegerField()
    total_bookings_ending = serializers.IntegerField()


class MonthlyAnalyticsResponseSerializer(serializers.Serializer):
    year = serializers.IntegerField()
    month = serializers.IntegerField()
    year_type = serializers.CharField()
    rows = MonthlyAnalyticsRowSerializer(many=True)
    summary = MonthlyAnalyticsSummarySerializer()


class InvoiceListSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(
        source="patient.get_full_name",
        read_only=True,
    )
    user_email = serializers.EmailField(
        source="user.email",
        read_only=True,
    )

    booking_order_id = serializers.CharField(
        source="secondary_order.order_id",
        read_only=True,
    )
    booking_venue = serializers.CharField(
        source="secondary_order.primary_order.venue.name",
        read_only=True,
        allow_null=True,
    )
    booking_locality = serializers.CharField(
        source="secondary_order.primary_order.venue.location.locality",
        read_only=True,
        allow_null=True,
    )
    booking_service = serializers.CharField(
        source="secondary_order.booking_service",
        read_only=True,
        allow_null=True,
    )
    booking_package = serializers.CharField(
        source="secondary_order.booking_package",
        read_only=True,
        allow_null=True,
    )
    issued_date = serializers.DateField(source="issue_date", read_only=True)

    class Meta:
        model = Invoice
        fields = (
            "id",
            "invoice_number",
            "booking_order_id",
            "booking_venue",
            "booking_locality",
            "booking_service",
            "booking_package",
            "secondary_order",
            "ternary_order",
            "patient",
            "patient_name",
            "user",
            "user_email",
            "period_start",
            "period_end",
            "total_amount",
            "paid_amount",
            "remaining_amount",
            "status",
            "due_date",
            "issue_date",
            "issued_date",
        )
