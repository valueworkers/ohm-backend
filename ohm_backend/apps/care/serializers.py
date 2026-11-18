"""
apps/care/serializers.py
────────────────────────
DRF serializers for the Care product module.
Re-exports core clinical, order, and catalogue serializers from `apps.billing.serializers`
to avoid duplicating model serializers across apps.
Defines Care-specific serializers (tenant payment configuration, attendance, razorpay).
"""

from rest_framework import serializers

# Re-export core serializers directly from apps.billing.serializers:
from apps.billing.serializers import (  # noqa: F401
    ContactBookingSerializer,
    LocationSerializer,
    PackageSerializer,
    PatientDocumentFileSerializer,
    PatientDocumentSerializer,
    PatientSerializer,
    PrimaryOrderSerializer,
    SecondaryOrderSerializer,
    TernaryOrderSerializer,
)

from .models import Attendance, AttendanceStatus, TenantPaymentConfig


# ---------------------------------------------------------------------------
# TenantPaymentConfig Serializer (Masks Sensitive Keys)
# ---------------------------------------------------------------------------

class TenantPaymentConfigSerializer(serializers.ModelSerializer):
    masked_key_id = serializers.SerializerMethodField()

    class Meta:
        model = TenantPaymentConfig
        fields = [
            "id",
            "razorpay_key_id",
            "razorpay_key_secret",
            "razorpay_webhook_secret",
            "currency",
            "masked_key_id",
            "is_deleted",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {
            "razorpay_key_secret": {"write_only": True},
            "razorpay_webhook_secret": {"write_only": True},
        }
        read_only_fields = ["id", "is_deleted", "created_at", "updated_at"]

    def get_masked_key_id(self, obj):
        if not obj.razorpay_key_id:
            return ""
        return f"{obj.razorpay_key_id[:6]}...{obj.razorpay_key_id[-4:]}" if len(obj.razorpay_key_id) > 10 else "***"


# ---------------------------------------------------------------------------
# Attendance Serializers
# ---------------------------------------------------------------------------

class AttendanceStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttendanceStatus
        fields = ["id", "code", "label", "is_deleted"]
        read_only_fields = ["id", "is_deleted"]


class AttendanceSerializer(serializers.ModelSerializer):
    user_name = serializers.ReadOnlyField(source="user.get_full_name")
    status_code = serializers.ReadOnlyField(source="status_record.code")

    class Meta:
        model = Attendance
        fields = [
            "id",
            "user",
            "user_name",
            "date",
            "status_record",
            "status_code",
            "duration",
            "reason",
            "is_deleted",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "is_deleted", "created_at", "updated_at"]


# ---------------------------------------------------------------------------
# Razorpay Transaction Serializers
# ---------------------------------------------------------------------------

class RazorpayInitiateSerializer(serializers.Serializer):
    invoice_id = serializers.IntegerField(required=True)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)


class RazorpayVerifySerializer(serializers.Serializer):
    razorpay_payment_id = serializers.CharField(max_length=120, required=True)
    razorpay_order_id = serializers.CharField(max_length=120, required=True)
    razorpay_signature = serializers.CharField(max_length=255, required=True)
    invoice_id = serializers.IntegerField(required=True)
