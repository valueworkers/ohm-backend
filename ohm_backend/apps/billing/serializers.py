"""
apps/billing/serializers.py
────────────────────────────
DRF serializers for all billing, quotation, order, and invoice models.
"""

from rest_framework import serializers

from .constants import PAYMENT_METHOD_NORMALIZE, QuoteLineKind, Uom
from .models import (
    ContactBooking,
    Invoice,
    InvoiceItem,
    Location,
    Package,
    Patient,
    PatientDocument,
    PatientDocumentFile,
    Payment,
    PrimaryOrder,
    Quote,
    QuoteEvent,
    QuoteLine,
    QuoteTaxSettings,
    SecondaryOrder,
    TernaryOrder,
)


# ---------------------------------------------------------------------------
# Patient & Location Serializers
# ---------------------------------------------------------------------------

class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = "__all__"


class PackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Package
        fields = "__all__"


class PatientDocumentFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientDocumentFile
        fields = "__all__"


class PatientDocumentSerializer(serializers.ModelSerializer):
    files = PatientDocumentFileSerializer(many=True, read_only=True)

    class Meta:
        model = PatientDocument
        fields = "__all__"


class PatientSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = "__all__"
        read_only_fields = ("patient_id", "registration_date", "updated_at")

    def get_full_name(self, obj):
        return obj.get_full_name()


class ContactBookingSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="patient.get_full_name", read_only=True, default=None)
    service_name = serializers.CharField(source="service.name", read_only=True, default=None)
    booked_by_email = serializers.CharField(source="booked_by.email", read_only=True, default=None)

    class Meta:
        model = ContactBooking
        fields = [
            "id", "patient", "patient_name", "booked_by", "booked_by_email",
            "service", "service_name", "address", "start_date", "end_date",
            "description", "created_at", "updated_at",
        ]
        read_only_fields = ("id", "created_at", "updated_at")

    def create(self, validated_data):
        request = self.context.get("request")
        if request and request.user.is_authenticated and "booked_by" not in validated_data:
            validated_data["booked_by"] = request.user
        return super().create(validated_data)


# ---------------------------------------------------------------------------
# Order Serializers
# ---------------------------------------------------------------------------

class TernaryOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = TernaryOrder
        fields = "__all__"
        read_only_fields = ("order_id", "subtotal", "booking_type")


class SecondaryOrderSerializer(serializers.ModelSerializer):
    ternary_orders = TernaryOrderSerializer(many=True, read_only=True)
    booking_service = serializers.ReadOnlyField()
    booking_package = serializers.ReadOnlyField()
    booking_venue = serializers.ReadOnlyField()

    class Meta:
        model = SecondaryOrder
        fields = "__all__"
        read_only_fields = ("order_id", "subtotal")


class PrimaryOrderSerializer(serializers.ModelSerializer):
    secondary_orders = SecondaryOrderSerializer(many=True, read_only=True)

    class Meta:
        model = PrimaryOrder
        fields = "__all__"
        read_only_fields = ("order_id", "total_bill", "booking_type")


# ---------------------------------------------------------------------------
# Quotation Serializers
# ---------------------------------------------------------------------------

class QuoteTaxSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteTaxSettings
        fields = "__all__"


class QuoteLineSerializer(serializers.ModelSerializer):
    position = serializers.IntegerField(required=False, min_value=1)
    uom = serializers.ChoiceField(choices=Uom.choices, default=Uom.PCS)
    kind = serializers.ChoiceField(choices=QuoteLineKind.choices, default=QuoteLineKind.CUSTOM)

    class Meta:
        model = QuoteLine
        fields = [
            "id", "position", "kind", "item", "specification", "hsn_sac",
            "uom", "qty", "unit_price", "discount_percent", "lead_time",
            "gst_rate", "taxable_value", "cgst_amount", "sgst_amount",
            "igst_amount", "line_total",
        ]
        read_only_fields = [
            "id", "taxable_value", "cgst_amount", "sgst_amount",
            "igst_amount", "line_total",
        ]


class QuoteEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor.email", read_only=True)

    class Meta:
        model = QuoteEvent
        fields = ["id", "kind", "detail", "actor_email", "at"]
        read_only_fields = ["id", "at"]


class QuoteSerializer(serializers.ModelSerializer):
    lines = QuoteLineSerializer(many=True, required=False)
    events = QuoteEventSerializer(many=True, read_only=True)
    number = serializers.CharField(max_length=30, required=False, allow_blank=True)
    valid_until = serializers.ReadOnlyField()
    state_tax_label = serializers.ReadOnlyField()
    patient_name = serializers.CharField(source="patient.get_full_name", read_only=True, default=None)
    company = serializers.CharField(max_length=200, required=False)
    buyer_email = serializers.EmailField(required=False)

    class Meta:
        model = Quote
        validators = []
        fields = [
            "id", "number", "revision", "status", "booking_entity",
            "venue", "service", "resource", "patient", "patient_name",
            "company", "attention", "address", "gstin", "state", "state_code",
            "buyer_email", "cc_emails", "bcc_emails", "ship_to", "your_ref",
            "quote_date", "valid_days", "valid_until", "notes", "terms",
            "supply_type", "state_tax_label", "place_of_supply_code",
            "seller_gstin", "seller_state_code", "seller_state_name",
            "subtotal", "discount_total", "freight", "packing", "charges_gst_rate",
            "taxable_amount", "cgst", "sgst", "igst", "round_off", "total",
            "accept_token", "sent_at", "accepted_at",
            "created_by", "created_by_email", "created_at",
            "lines", "events",
        ]
        read_only_fields = [
            "id", "revision", "status", "quote_date", "state_code",
            "supply_type", "place_of_supply_code", "seller_gstin",
            "seller_state_code", "seller_state_name", "subtotal",
            "discount_total", "taxable_amount", "cgst", "sgst", "igst",
            "round_off", "total", "accept_token", "sent_at", "accepted_at",
            "created_by", "created_by_email", "created_at",
        ]

    def to_representation(self, instance):
        if not instance.pk:
            fields = [f for f in self.fields.values() if f.field_name not in ["lines", "events"]]
            ret = {}
            for field in fields:
                try:
                    attribute = field.get_attribute(instance)
                    ret[field.field_name] = field.to_representation(attribute) if attribute is not None else None
                except Exception:
                    ret[field.field_name] = None
            ret["lines"] = []
            ret["events"] = []
            return ret
        return super().to_representation(instance)

    def validate(self, attrs):
        patient = attrs.get("patient", getattr(self.instance, "patient", None))
        if patient:
            if not attrs.get("company") and not getattr(self.instance, "company", ""):
                attrs["company"] = patient.get_full_name()
            if not attrs.get("buyer_email") and not getattr(self.instance, "buyer_email", ""):
                attrs["buyer_email"] = patient.email or "patient@example.com"
            if not attrs.get("address") and not getattr(self.instance, "address", ""):
                attrs["address"] = patient.address or ""
        if not attrs.get("company") and not getattr(self.instance, "company", ""):
            raise serializers.ValidationError({"company": "Company or patient name is required."})
        if not attrs.get("buyer_email") and not getattr(self.instance, "buyer_email", ""):
            raise serializers.ValidationError({"buyer_email": "Buyer or patient email is required."})
        return super().validate(attrs)

    def create(self, validated_data):
        lines_data = validated_data.pop("lines", [])
        from .services import create_quote
        request = self.context.get("request")
        user = request.user if request and request.user.is_authenticated else None
        return create_quote(
            company=validated_data.pop("company"),
            buyer_email=validated_data.pop("buyer_email"),
            state=validated_data.pop("state", "Karnataka"),
            lines=lines_data,
            user=user,
            **validated_data,
        )

    def update(self, instance, validated_data):
        lines_data = validated_data.pop("lines", None)
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        instance.save()

        if lines_data is not None:
            instance.lines.all().delete()
            for idx, l in enumerate(lines_data, start=1):
                QuoteLine.objects.create(quote=instance, position=idx, **l)

        from .services import recalculate_quote
        recalculate_quote(instance)
        return instance


class SendEmailSerializer(serializers.Serializer):
    recipients = serializers.ListField(
        child=serializers.EmailField(),
        required=False,
        help_text="Optional test or override recipient list.",
    )


# ---------------------------------------------------------------------------
# Invoice & Payment Serializers
# ---------------------------------------------------------------------------

class InvoiceItemSerializer(serializers.ModelSerializer):
    uom = serializers.ChoiceField(choices=Uom.choices, default=Uom.PCS)

    class Meta:
        model = InvoiceItem
        fields = [
            "id", "position", "line_type", "description", "hsn_sac",
            "specification", "quantity", "uom", "unit_price",
            "discount_percent", "lead_time", "gst_rate",
            "discount_amount", "taxable_value", "cgst_amount",
            "sgst_amount", "igst_amount", "amount",
        ]
        read_only_fields = [
            "id", "discount_amount", "taxable_value",
            "cgst_amount", "sgst_amount", "igst_amount", "amount",
        ]


class PaymentSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="patient.get_full_name", read_only=True, default=None)
    invoice_number = serializers.CharField(source="invoice.invoice_number", read_only=True, default=None)

    class Meta:
        model = Payment
        fields = "__all__"
        read_only_fields = ("reference", "created_at", "updated_at")

    def to_internal_value(self, data):
        if isinstance(data, dict) and "method" in data:
            data = data.copy()
            raw_method = str(data["method"]).strip().lower()
            normalized = PAYMENT_METHOD_NORMALIZE.get(raw_method)
            if normalized:
                data["method"] = normalized
            elif isinstance(data["method"], str):
                data["method"] = data["method"].upper()
        return super().to_internal_value(data)


class InvoiceSerializer(serializers.ModelSerializer):
    items = InvoiceItemSerializer(many=True, required=False)
    payments = PaymentSerializer(many=True, read_only=True)
    balance_due = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    invoice_type = serializers.CharField(read_only=True)
    patient_name = serializers.CharField(source="patient.get_full_name", read_only=True, default=None)

    class Meta:
        model = Invoice
        fields = [
            "id", "invoice_number", "status", "invoice_type_tag", "invoice_type",
            "booking_entity", "venue", "service", "resource",
            "quote", "secondary_order", "ternary_order", "parent_invoice",
            "patient", "patient_name", "user",
            "customer_name", "attention", "address", "buyer_email",
            "cc_emails", "bcc_emails", "gstin", "state", "state_code",
            "is_interstate", "ship_to", "your_ref", "issuer",
            "issue_date", "due_days", "due_date", "period_start", "period_end",
            "currency", "subtotal", "discount_amount", "premium_amount",
            "taxable_amount", "cgst_amount", "sgst_amount", "igst_amount",
            "tax_amount", "round_off", "total_amount", "amount_received",
            "paid_amount", "remaining_amount", "balance_due",
            "terms", "notes", "pdf", "sent_at",
            "created_at", "updated_at",
            "items", "payments",
        ]
        read_only_fields = [
            "id", "invoice_number", "invoice_type", "state_code",
            "is_interstate", "subtotal", "discount_amount", "taxable_amount",
            "cgst_amount", "sgst_amount", "igst_amount", "tax_amount",
            "round_off", "total_amount", "paid_amount", "remaining_amount",
            "balance_due", "sent_at", "created_at", "updated_at",
        ]

    def to_representation(self, instance):
        if not instance.pk:
            fields = [f for f in self.fields.values() if f.field_name not in ["items", "payments"]]
            ret = {}
            for field in fields:
                try:
                    attribute = field.get_attribute(instance)
                    ret[field.field_name] = field.to_representation(attribute) if attribute is not None else None
                except Exception:
                    ret[field.field_name] = None
            ret["items"] = []
            ret["payments"] = []
            return ret
        return super().to_representation(instance)

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])
        invoice = Invoice(**validated_data)
        from .services import save_invoice
        return save_invoice(invoice, items_data)

    def update(self, instance, validated_data):
        items_data = validated_data.pop("items", None)
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        from .services import save_invoice
        return save_invoice(instance, items_data)
