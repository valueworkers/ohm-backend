import logging
from decimal import Decimal
from django.db import models, transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.billing.models import (
    ContactBooking,
    Invoice,
    Location,
    Package,
    Patient,
    PatientDocument,
    Payment,
    PrimaryOrder,
    SecondaryOrder,
    TernaryOrder,
)
from config.pagination import StandardResultsSetPagination
from .constants import BookingStatus
from .models import (
    Attendance,
    AttendanceStatus,
    TenantPaymentConfig,
)
from .permissions import CareModelPermissions, HasProductAccess, MedicareModelPermissions
from .serializers import (
    AttendanceSerializer,
    AttendanceStatusSerializer,
    ContactBookingSerializer,
    LocationSerializer,
    PackageSerializer,
    PatientDocumentSerializer,
    PatientSerializer,
    PrimaryOrderSerializer,
    RazorpayInitiateSerializer,
    RazorpayVerifySerializer,
    SecondaryOrderSerializer,
    TenantPaymentConfigSerializer,
    TernaryOrderSerializer,
)
from .services import CareInvoiceService, AttendanceService, RazorpayService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base ViewSet with Plan Gating, RBAC, Soft Deletion, and Shared Pagination
# ---------------------------------------------------------------------------

class BaseMedicareViewSet(viewsets.ModelViewSet):
    """
    Standard Base ViewSet for the Care product:
      - Plan-gated: HasProductAccess checks tenant feature entitlement
      - RBAC: CareModelPermissions enforces Django model permissions (including view on GET)
      - StandardResultsSetPagination
      - Filtering: excludes soft-deleted records (is_deleted=False) where applicable
      - Soft delete on DELETE HTTP methods
      - 'active-status' PATCH action for toggling or updating the status/is_active flag
    """

    product_code = "care"
    permission_classes = [IsAuthenticated, HasProductAccess, CareModelPermissions]
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]

    def get_queryset(self):
        qs = super().get_queryset()
        if hasattr(qs.model, "is_deleted"):
            return qs.filter(is_deleted=False)
        return qs

    def perform_destroy(self, instance):
        if hasattr(instance, "soft_delete"):
            instance.soft_delete()
        else:
            instance.delete()

    @action(detail=True, methods=["patch"], url_path="active-status")
    def active_status(self, request, pk=None):
        """Toggle or explicitly update the active status flag."""
        instance = self.get_object()
        req_status = request.data.get("status")
        if req_status is not None:
            new_status = bool(req_status)
        else:
            if hasattr(instance, "is_active"):
                new_status = not instance.is_active
            elif hasattr(instance, "status") and isinstance(instance.status, bool):
                new_status = not instance.status
            else:
                new_status = True

        if hasattr(instance, "is_active"):
            instance.is_active = new_status
            instance.save(update_fields=["is_active"])
            return Response({"id": instance.id, "status": instance.is_active})
        elif hasattr(instance, "status") and isinstance(instance._meta.get_field("status"), models.BooleanField):
            instance.status = new_status
            instance.save(update_fields=["status"])
            return Response({"id": instance.id, "status": instance.status})
        return Response({"id": instance.id})


# ---------------------------------------------------------------------------
# Patient ViewSets
# ---------------------------------------------------------------------------

class PatientViewSet(BaseMedicareViewSet):
    queryset = Patient.objects.all().select_related("registered_by").prefetch_related("documents__files")
    serializer_class = PatientSerializer
    search_fields = ["first_name", "last_name", "email", "phone", "patient_id"]
    filterset_fields = ["gender", "blood_group", "is_probono", "is_registration_fees_paid", "is_active"]
    ordering_fields = ["registration_date", "first_name", "last_name", "age"]
    ordering = ["-registration_date"]


class PatientDocumentViewSet(BaseMedicareViewSet):
    queryset = PatientDocument.objects.all().select_related("patient", "uploaded_by").prefetch_related("files")
    serializer_class = PatientDocumentSerializer
    filterset_fields = ["patient"]
    search_fields = ["title", "remarks"]


# ---------------------------------------------------------------------------
# Location & Package ViewSets
# ---------------------------------------------------------------------------

class LocationViewSet(BaseMedicareViewSet):
    queryset = Location.objects.all()
    serializer_class = LocationSerializer
    filterset_fields = ["location_type", "city", "state"]
    search_fields = ["building_name", "locality", "city", "state"]


class PackageViewSet(BaseMedicareViewSet):
    queryset = Package.objects.all()
    serializer_class = PackageSerializer
    filterset_fields = ["package_type", "period", "is_active"]
    search_fields = ["name", "description"]


# ---------------------------------------------------------------------------
# Booking Order ViewSets
# ---------------------------------------------------------------------------

class PrimaryOrderViewSet(BaseMedicareViewSet):
    queryset = PrimaryOrder.objects.all().select_related("patient", "user")
    serializer_class = PrimaryOrderSerializer
    filterset_fields = ["status", "booking_type", "booking_entity", "patient"]
    search_fields = ["order_id", "patient__first_name", "patient__last_name"]

    def perform_create(self, serializer):
        if "user" not in serializer.validated_data and self.request.user.is_authenticated:
            serializer.save(user=self.request.user)
        else:
            serializer.save()


class SecondaryOrderViewSet(BaseMedicareViewSet):
    queryset = SecondaryOrder.objects.all().select_related(
        "primary_order", "primary_order__patient", "package", "service", "venue"
    )
    serializer_class = SecondaryOrderSerializer
    filterset_fields = [
        "status",
        "is_registration_fee",
        "primary_order",
        "package",
        "service",
        "venue",
        "booking_type",
    ]
    search_fields = ["order_id", "primary_order__order_id"]

    def perform_create(self, serializer):
        instance = serializer.save()
        try:
            instance.recalculate_subtotal()
        except Exception:
            pass

    @action(detail=True, methods=["post"], url_path="generate-invoice")
    def generate_invoice(self, request, pk=None):
        """Generate or return the Invoice for this SecondaryOrder."""
        secondary = self.get_object()
        try:
            invoice = CareInvoiceService.generate_invoice_for_secondary(secondary)
            return Response({
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "total_amount": str(invoice.total_amount),
                "remaining_amount": str(invoice.remaining_amount),
                "status": invoice.status,
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error("Failed to generate invoice for secondary order %s: %s", secondary.pk, e, exc_info=True)
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class TernaryOrderViewSet(BaseMedicareViewSet):
    queryset = TernaryOrder.objects.all().select_related("secondary_order")
    serializer_class = TernaryOrderSerializer
    filterset_fields = ["status", "entity_type"]

    def perform_create(self, serializer):
        instance = serializer.save()
        if instance.secondary_order:
            try:
                instance.secondary_order.recalculate_subtotal()
            except Exception:
                pass

    @action(detail=True, methods=["post"], url_path="generate-invoice")
    def generate_invoice(self, request, pk=None):
        """Generate or return the Invoice for this TernaryOrder add-on."""
        ternary = self.get_object()
        try:
            invoice = CareInvoiceService.generate_invoice_for_ternary(ternary)
            return Response({
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "total_amount": str(invoice.total_amount),
                "remaining_amount": str(invoice.remaining_amount),
                "status": invoice.status,
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error("Failed to generate invoice for ternary order %s: %s", ternary.pk, e, exc_info=True)
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ContactBookingViewSet(BaseMedicareViewSet):
    queryset = ContactBooking.objects.all().select_related("patient", "booked_by", "service")
    serializer_class = ContactBookingSerializer
    filterset_fields = ["patient", "service", "start_date", "end_date"]
    search_fields = ["address", "description", "patient__first_name", "patient__last_name"]
    ordering_fields = ["start_date", "end_date", "created_at"]
    ordering = ["-start_date"]


# ---------------------------------------------------------------------------
# Tenant Payment Config ViewSet
# ---------------------------------------------------------------------------

class TenantPaymentConfigViewSet(BaseMedicareViewSet):
    queryset = TenantPaymentConfig.objects.all()
    serializer_class = TenantPaymentConfigSerializer

    def get_object(self):
        config, _ = TenantPaymentConfig.objects.get_or_create(
            defaults={
                "razorpay_key_id": "",
                "razorpay_key_secret": "",
                "currency": "INR",
            }
        )
        return config

    def list(self, request, *args, **kwargs):
        config = self.get_object()
        serializer = self.get_serializer(config)
        return Response(serializer.data)

    def partial_update_singleton(self, request, *args, **kwargs):
        config = self.get_object()
        serializer = self.get_serializer(config, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def update_singleton(self, request, *args, **kwargs):
        config = self.get_object()
        serializer = self.get_serializer(config, data=request.data, partial=False)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# Attendance ViewSets
# ---------------------------------------------------------------------------

class AttendanceStatusViewSet(BaseMedicareViewSet):
    queryset = AttendanceStatus.objects.all()
    serializer_class = AttendanceStatusSerializer
    filterset_fields = ["code"]
    search_fields = ["code", "label"]


class AttendanceViewSet(BaseMedicareViewSet):
    queryset = Attendance.objects.all().select_related("user", "status_record")
    serializer_class = AttendanceSerializer
    filterset_fields = ["user", "date", "status_record"]
    ordering_fields = ["date", "created_at"]
    ordering = ["-date"]

    @action(detail=False, methods=["post"], url_path="mark-today")
    def mark_today(self, request):
        """Batch creates present attendance records for all active staff for the day."""
        res = AttendanceService.mark_staff_present_today()
        return Response(res, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="bulk-mark-present")
    def bulk_mark_present(self, request):
        """Celery-triggerable or admin bulk mark present for today."""
        from .tasks import run_mark_attendance_today
        task = run_mark_attendance_today.delay(schema_name=request.tenant.schema_name)
        return Response({"task_id": task.id, "status": "enqueued"}, status=status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------------------
# Razorpay Payment Gateway ViewSet
# ---------------------------------------------------------------------------

class CareRazorpayViewSet(viewsets.ViewSet):
    """
    Direct payment initiation and webhook/client verification using
    per-tenant credentials configured in TenantPaymentConfig.
    """

    permission_classes = [IsAuthenticated, HasProductAccess]
    product_code = "care"

    @action(detail=False, methods=["post"])
    def initiate(self, request):
        serializer = RazorpayInitiateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            invoice = Invoice.objects.get(pk=data["invoice_id"])
        except Invoice.DoesNotExist:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)

        amount = data.get("amount") or invoice.remaining_amount
        try:
            result = RazorpayService.initiate_order(invoice, amount)
            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error("Razorpay initiation failed: %s", e, exc_info=True)
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"])
    def verify(self, request):
        serializer = RazorpayVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            invoice = Invoice.objects.get(pk=data["invoice_id"])
        except Invoice.DoesNotExist:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)

        verified = RazorpayService.verify_payment(
            invoice=invoice,
            razorpay_payment_id=data["razorpay_payment_id"],
            razorpay_order_id=data["razorpay_order_id"],
            razorpay_signature=data["razorpay_signature"],
        )
        if verified:
            return Response({
                "status": "success",
                "message": "Payment verified and invoice updated successfully.",
                "invoice_status": invoice.status,
                "paid_amount": str(invoice.paid_amount),
                "remaining_amount": str(invoice.remaining_amount),
            }, status=status.HTTP_200_OK)
        return Response({"status": "failed", "detail": "Invalid signature or verification failure."}, status=status.HTTP_400_BAD_REQUEST)


# Backwards-compatible alias
MedicareRazorpayViewSet = CareRazorpayViewSet
