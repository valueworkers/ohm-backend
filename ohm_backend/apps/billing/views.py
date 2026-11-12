"""
apps/billing/views.py
─────────────────────
API views for the billing, quotation, order, and invoice domain.
"""

from decimal import Decimal
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .constants import PAYMENT_METHOD_NORMALIZE

from .models import (
    ContactBooking,
    Invoice,
    Location,
    Package,
    Patient,
    PatientDocument,
    Payment,
    PrimaryOrder,
    Quote,
    QuoteTaxSettings,
    SecondaryOrder,
    TernaryOrder,
)
from .serializers import (
    ContactBookingSerializer,
    InvoiceSerializer,
    LocationSerializer,
    PackageSerializer,
    PatientDocumentSerializer,
    PatientSerializer,
    PaymentSerializer,
    PrimaryOrderSerializer,
    QuoteSerializer,
    QuoteTaxSettingsSerializer,
    SecondaryOrderSerializer,
    SendEmailSerializer,
    TernaryOrderSerializer,
)
from .services import (
    create_invoice_from_quote,
    issue_invoice,
    recalculate_quote,
    record_payment,
    respond_to_quote,
    revise_quote,
    send_invoice_email,
    send_quote,
    void_invoice,
)


# ---------------------------------------------------------------------------
# Patient & Location ViewSets
# ---------------------------------------------------------------------------

class LocationViewSet(viewsets.ModelViewSet):
    queryset = Location.objects.all()
    serializer_class = LocationSerializer


class PackageViewSet(viewsets.ModelViewSet):
    queryset = Package.objects.all()
    serializer_class = PackageSerializer


class PatientViewSet(viewsets.ModelViewSet):
    queryset = Patient.objects.filter(is_deleted=False)
    serializer_class = PatientSerializer
    search_fields = ("first_name", "last_name", "phone", "email", "patient_id")
    filterset_fields = ("gender", "is_active", "is_probono")

    @action(detail=True, methods=["post"])
    def soft_delete(self, request, pk=None):
        patient = self.get_object()
        patient.soft_delete()
        return Response({"status": "deleted"})


class PatientDocumentViewSet(viewsets.ModelViewSet):
    queryset = PatientDocument.objects.all()
    serializer_class = PatientDocumentSerializer


class ContactBookingViewSet(viewsets.ModelViewSet):
    queryset = ContactBooking.objects.select_related("patient", "booked_by", "service")
    serializer_class = ContactBookingSerializer
    filterset_fields = ("patient", "service", "start_date", "end_date")
    search_fields = ("address", "description", "patient__first_name", "patient__last_name", "patient__phone")
    ordering_fields = ("start_date", "end_date", "created_at")
    ordering = ("-start_date",)


# ---------------------------------------------------------------------------
# Order ViewSets
# ---------------------------------------------------------------------------

class PrimaryOrderViewSet(viewsets.ModelViewSet):
    queryset = PrimaryOrder.objects.select_related("patient", "package", "user")
    serializer_class = PrimaryOrderSerializer
    filterset_fields = ("status", "booking_type", "booking_entity", "patient")
    search_fields = ("order_id", "patient__first_name", "patient__last_name")


class SecondaryOrderViewSet(viewsets.ModelViewSet):
    queryset = SecondaryOrder.objects.select_related("primary_order")
    serializer_class = SecondaryOrderSerializer
    filterset_fields = ("status", "is_registration_fee")


class TernaryOrderViewSet(viewsets.ModelViewSet):
    queryset = TernaryOrder.objects.select_related("secondary_order", "package")
    serializer_class = TernaryOrderSerializer
    filterset_fields = ("status", "booking_type")


# ---------------------------------------------------------------------------
# Quotation ViewSets
# ---------------------------------------------------------------------------

class QuoteTaxSettingsViewSet(viewsets.ModelViewSet):
    queryset = QuoteTaxSettings.objects.all()
    serializer_class = QuoteTaxSettingsSerializer


class QuoteViewSet(viewsets.ModelViewSet):
    queryset = Quote.objects.prefetch_related("lines", "events").select_related(
        "patient", "created_by", "venue", "service", "resource"
    )
    serializer_class = QuoteSerializer
    filterset_fields = ("status", "booking_entity", "company", "state", "patient")
    search_fields = ("number", "company", "buyer_email", "gstin", "patient__first_name", "patient__last_name")

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        """Send this quote to the client by email."""
        quote = self.get_object()
        quote = send_quote(quote, user=request.user)
        return Response(self.get_serializer(quote).data)

    @action(detail=True, methods=["post"])
    def send_test(self, request, pk=None):
        """Send a test email for this quote to specified recipients."""
        serializer = SendEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        quote = self.get_object()
        quote = send_quote(
            quote,
            user=request.user,
            test_recipients=serializer.validated_data.get("recipients"),
        )
        return Response(self.get_serializer(quote).data)

    @action(detail=True, methods=["post"])
    def revise(self, request, pk=None):
        """Create a new revision of this quote."""
        quote = self.get_object()
        revised = revise_quote(quote, user=request.user)
        return Response(self.get_serializer(revised).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def recalculate(self, request, pk=None):
        """Recalculate GST line items and totals."""
        quote = self.get_object()
        quote = recalculate_quote(quote)
        return Response(self.get_serializer(quote).data)

    @action(detail=True, methods=["post"])
    def convert_to_invoice(self, request, pk=None):
        """Convert this quotation into a formal Invoice."""
        quote = self.get_object()
        issue_now = request.data.get("issue_now", False)
        invoice = create_invoice_from_quote(quote, issue_now=bool(issue_now))
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Invoice & Payment ViewSets
# ---------------------------------------------------------------------------

class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = Invoice.objects.select_related(
        "patient", "user", "quote", "secondary_order", "ternary_order",
        "venue", "service", "resource"
    ).prefetch_related("items", "payments")
    serializer_class = InvoiceSerializer
    filterset_fields = ("status", "invoice_type_tag", "booking_entity", "is_interstate", "patient")
    search_fields = ("invoice_number", "customer_name", "buyer_email", "gstin", "patient__first_name", "patient__last_name")

    @action(detail=True, methods=["post"])
    def issue(self, request, pk=None):
        """Issue a draft invoice."""
        invoice = self.get_object()
        invoice = issue_invoice(invoice)
        return Response(InvoiceSerializer(invoice).data)

    @action(detail=True, methods=["post"], url_path="raise")
    def raise_invoice(self, request, pk=None):
        """Alias for issue to match platform invoice conventions."""
        return self.issue(request, pk=pk)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        """Mark an invoice as VOID."""
        invoice = self.get_object()
        invoice = void_invoice(invoice)
        return Response(InvoiceSerializer(invoice).data)

    @action(detail=True, methods=["post"])
    def recalculate(self, request, pk=None):
        """Recalculate line item totals and payments."""
        invoice = self.get_object()
        invoice.recalculate_from_items()
        invoice.recalculate_payments()
        return Response(InvoiceSerializer(invoice).data)

    @action(detail=True, methods=["post"], url_path="record-payment")
    def record_payment(self, request, pk=None):
        """Record a manual payment against this invoice."""
        invoice = self.get_object()
        amount = Decimal(str(request.data.get("amount", 0)))
        raw_method = str(request.data.get("method", "CASH")).strip().lower()
        method = PAYMENT_METHOD_NORMALIZE.get(raw_method, raw_method.upper())
        reference = request.data.get("reference", "")

        payment = record_payment(
            invoice,
            amount=amount,
            method=method,
            reference=reference,
            user=request.user if request.user.is_authenticated else None,
        )
        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="record_payment")
    def record_payment_alias(self, request, pk=None):
        """Backward-compatible snake_case alias for record-payment."""
        return self.record_payment(request, pk=pk)

    @action(detail=True, methods=["post"])
    def send_email(self, request, pk=None):
        """
        Send this invoice via email.
        Accepts optional `recipients` list in request body.
        """
        invoice = self.get_object()
        serializer = SendEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recipients = serializer.validated_data.get("recipients")

        invoice = send_invoice_email(
            invoice,
            user=request.user if request.user.is_authenticated else None,
            test_recipients=recipients,
        )
        return Response({
            "status": "sent",
            "invoice_number": invoice.invoice_number,
            "sent_at": invoice.sent_at,
        })


class PaymentViewSet(viewsets.ModelViewSet):
    queryset = Payment.objects.select_related("invoice", "patient")
    serializer_class = PaymentSerializer
    filterset_fields = ("method", "is_verified", "mapping_status", "patient", "invoice")
    search_fields = (
        "reference", "gateway_order_id", "gateway_payment_id",
        "patient__first_name", "patient__last_name", "invoice__invoice_number",
    )

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        payment = self.get_object()
        changed = payment.verify()
        return Response({"changed": changed, "is_verified": payment.is_verified})

    @action(detail=True, methods=["post"])
    def unverify(self, request, pk=None):
        payment = self.get_object()
        changed = payment.unverify()
        return Response({"changed": changed, "is_verified": payment.is_verified})
