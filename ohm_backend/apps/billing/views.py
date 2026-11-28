"""
apps/billing/views.py
─────────────────────
API views for the billing, quotation, order, and invoice domain.
"""

from decimal import Decimal
from django.db.models import CharField, Count, DateTimeField, IntegerField, Max, Min
from django.db.models.functions import Cast, Coalesce
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from .constants import PAYMENT_METHOD_NORMALIZE

from .models import (
    Invoice,
    Location,
    Package,
    Patient,
    PatientDocument,
    PatientDocumentFile,
    Payment,
    Quote,
    QuoteTaxSettings,
)
from .serializers import (
    InvoiceSerializer,
    LocationSerializer,
    PackageSerializer,
    PatientSerializer,
    PatientDocumentSerializer,
    PaymentSerializer,
    QuoteSerializer,
    QuoteTaxSettingsSerializer,
    SendEmailSerializer,
)
from .filters import PatientFilter
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
# Quotation ViewSets
# ---------------------------------------------------------------------------

class LocationViewSet(viewsets.ModelViewSet):
    """Manage care locations inside the active tenant schema."""

    queryset = Location.objects.select_related("user").all()
    serializer_class = LocationSerializer
    filterset_fields = ("user", "location_type", "city", "state")
    search_fields = ("building_name", "address_line1", "locality", "city", "state", "postal_code")


class PackageViewSet(viewsets.ModelViewSet):
    """Manage care packages inside the active tenant schema."""

    queryset = Package.objects.select_related("owner", "content_type").all()
    serializer_class = PackageSerializer
    filterset_fields = ("owner", "content_type", "object_id", "package_type", "period", "is_active")
    search_fields = ("name", "description")


class PatientDocumentViewSet(viewsets.ModelViewSet):
    """Manage patient documents and uploaded files inside the active tenant schema."""

    serializer_class = PatientDocumentSerializer
    queryset = PatientDocument.objects.select_related("patient", "uploaded_by").prefetch_related("files")
    parser_classes = (MultiPartParser, FormParser)
    filterset_fields = ("patient",)
    search_fields = ("title", "remarks", "patient__first_name", "patient__last_name", "patient__patient_id")

    def perform_create(self, serializer):
        document = serializer.save(uploaded_by=self.request.user)
        files = self.request.FILES.getlist("files")
        PatientDocumentFile.objects.bulk_create(
            [PatientDocumentFile(document=document, file=file) for file in files]
        )

    def perform_update(self, serializer):
        document = serializer.save()
        files = self.request.FILES.getlist("files")
        PatientDocumentFile.objects.bulk_create(
            [PatientDocumentFile(document=document, file=file) for file in files]
        )


class PatientViewSet(viewsets.ModelViewSet):
    serializer_class = PatientSerializer
    filterset_class = PatientFilter
    search_fields = [
        "=id",
        "patient_id",
        "first_name",
        "last_name",
        "=email",
        "=phone",
    ]
    ordering_fields = [
        "id",
        "patient_id_num",
        "first_name",
        "last_name",
        "email",
        "phone",
        "address",
        "age",
        "emergency_contact",
        "emergency_phone",
        "emergency_contact_2",
        "emergency_phone_2",
        "gender",
        "onboading_date",
        "is_deleted",
        "is_active",
        "location_type",
        "emr_count",
        "affiliate",
        "source",
        "referred_by",
        "is_probono",
    ]
    ordering = ["first_name", "last_name"]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser or user.is_owner:
            queryset = Patient.objects.all()
        else:
            queryset = Patient.objects.filter(registered_by=user)

        return queryset.annotate(
            emr_count=Count("documents", distinct=True),
            location_type=Max("primaryorder__booking_type"),
            booking_locality=Coalesce(
                Max("primaryorder__secondary_orders__venue__location__locality"),
                Max(Cast("primaryorder__secondary_orders__client_address", CharField())),
                output_field=CharField(),
            ),
            onboading_date=Coalesce(
                Min("primaryorder__start_datetime"),
                "registration_date",
                output_field=DateTimeField(),
            ),
            patient_id_num=Cast("patient_id", IntegerField()),
        )

    def perform_create(self, serializer):
        serializer.save(registered_by=self.request.user, is_active=True)

    def perform_destroy(self, instance):
        if hasattr(instance, "soft_delete"):
            instance.soft_delete()
        else:
            instance.delete()

    @action(detail=True, methods=["patch"], url_path="active-status")
    def active_status(self, request, pk=None):
        patient = self.get_object()
        is_active = request.data.get("is_active")
        if not isinstance(is_active, bool):
            return Response(
                {"detail": "'is_active' (boolean) is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        patient.is_active = is_active
        patient.save(update_fields=["is_active", "updated_at"])
        return Response({"status": patient.is_active}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"])
    def patient_dropdown(self, request):
        queryset = self.filter_queryset(self.get_queryset())
        queryset = queryset.select_related("registered_by").only(
            "id",
            "first_name",
            "last_name",
            "phone",
            "registered_by__id",
            "registered_by__first_name",
            "registered_by__last_name",
        )
        data = [
            {
                "id": patient.id,
                "name": patient.get_full_name(),
                "phone": patient.phone,
                "registered_by": (
                    patient.registered_by.get_full_name()
                    if patient.registered_by_id
                    else None
                ),
            }
            for patient in queryset
        ]
        return Response(data, status=status.HTTP_200_OK)


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
