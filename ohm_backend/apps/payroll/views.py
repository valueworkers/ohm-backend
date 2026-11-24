from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction as db_transaction
from django.db.models import OuterRef, Subquery, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Attendance,
    AttendanceStatus,
    SalaryReport,
    SalaryStructure,
    SalaryTransaction,
)
from .permissions import (
    CanViewSalaryReport,
    IsOwnerOrReadOnly,
    IsPayrollAdminOrSelfReadOnly,
    RootAdminThrottleExemptMixin,
    is_admin_or_manager,
)
from .serializers import (
    AttendanceSerializer,
    AttendanceStatusSerializer,
    EmployeePayrollListSerializer,
    SalaryReportSerializer,
    SalaryStructureSerializer,
    SalaryTransactionCreateSerializer,
    SalaryTransactionSerializer,
)
from .tasks import queue_salary_report_refresh

User = get_user_model()


class AttendanceCursorPagination(CursorPagination):
    page_size = 25
    ordering = ("-date", "id")
    cursor_query_param = "cursor"


class EmployeePayrollViewSet(RootAdminThrottleExemptMixin, viewsets.ReadOnlyModelViewSet):
    """
    Read-only payroll summary list of employees.
    """
    serializer_class = EmployeePayrollListSerializer
    permission_classes = [IsAuthenticated]

    filterset_fields = [
        "id",
        "first_name",
        "last_name",
        "employee_profile__employee_id",
        "email",
        "mobile_number",
        "employee_profile__category",
        "employee_profile__vendor_name",
    ]

    search_fields = [
        "first_name",
        "last_name",
        "employee_profile__employee_id",
        "employee_profile__vendor_name",
        "=email",
        "=mobile_number",
    ]

    ordering_fields = [
        "id",
        "first_name",
        "last_name",
        "basic_salary",
        "pf_amount",
        "esi_amount",
        "effective_date",
        "recent_payment",
    ]
    ordering = ["first_name", "last_name"]

    def get_queryset(self):
        user = self.request.user

        latest_salary = SalaryStructure.objects.filter(
            user=OuterRef("pk"),
        ).order_by("-effective_from", "-pk")

        latest_txn = SalaryTransaction.objects.filter(
            salary_report__user=OuterRef("pk"),
            status="SUCCESS",
        ).order_by("-processed_at", "-created_at")

        manager = getattr(User.objects, "employees", User.objects.all)
        base_qs = (
            manager()
            .select_related("employee_profile")
            .annotate(
                basic_salary=Subquery(latest_salary.values("final_salary")[:1]),
                pf_amount=Subquery(latest_salary.values("pf_amount")[:1]),
                esi_amount=Subquery(latest_salary.values("esi_amount")[:1]),
                effective_date=Subquery(latest_salary.values("effective_from")[:1]),
                recent_payment=Subquery(latest_txn.values("amount_paid")[:1]),
                payout_mode=Subquery(latest_txn.values("payment_method")[:1]),
            )
        )

        if is_admin_or_manager(user):
            return base_qs
        return base_qs.filter(id=user.id)


class SalaryStructureViewSet(RootAdminThrottleExemptMixin, viewsets.ModelViewSet):
    """
    ViewSet for managing salary structures
    """
    serializer_class = SalaryStructureSerializer
    permission_classes = [IsPayrollAdminOrSelfReadOnly]

    filterset_fields = [
        "user_id",
        "change_type",
        "effective_from",
    ]

    search_fields = [
        "user__first_name",
        "user__last_name",
        "=user__email",
        "=user__mobile_number",
    ]

    def get_queryset(self):
        user = self.request.user
        if is_admin_or_manager(user):
            queryset = SalaryStructure.objects.all()
        else:
            queryset = SalaryStructure.objects.filter(user=user)
        return queryset.select_related("user").order_by("-effective_from")


class AttendanceStatusViewSet(RootAdminThrottleExemptMixin, viewsets.ModelViewSet):
    serializer_class = AttendanceStatusSerializer
    permission_classes = [IsOwnerOrReadOnly]

    def get_queryset(self):
        return AttendanceStatus.objects.all()

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class AttendanceView(RootAdminThrottleExemptMixin, APIView):
    """
    GET: List all attendance records with filters
    POST: Create or Update attendance
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if is_admin_or_manager(user):
            queryset = Attendance.objects.all()
        else:
            queryset = Attendance.objects.filter(user=user)

        user_id = request.query_params.get("user_id")
        if user_id:
            queryset = queryset.filter(user_id=user_id)

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        if start_date:
            queryset = queryset.filter(date__gte=start_date)
        if end_date:
            queryset = queryset.filter(date__lte=end_date)

        status_code = request.query_params.get("status")
        if status_code:
            queryset = queryset.filter(status__code=status_code)

        date_val = request.query_params.get("date")
        if date_val:
            queryset = queryset.filter(date=date_val)

        queryset = queryset.select_related("user", "status").order_by("-date")
        serializer = AttendanceSerializer(queryset, many=True)

        return Response({
            "count": queryset.count(),
            "results": serializer.data,
        }, status=status.HTTP_200_OK)

    def post(self, request):
        if not is_admin_or_manager(request.user):
            return Response(
                {"detail": "Only payroll administrators can record attendance."},
                status=status.HTTP_403_FORBIDDEN,
            )
        user_id = request.data.get("user")
        date_val = request.data.get("date")

        if not user_id:
            return Response({"error": "user field is required"}, status=status.HTTP_400_BAD_REQUEST)
        if not date_val:
            return Response({"error": "date field is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            attendance = Attendance.objects.select_related("user", "status").get(
                user_id=user_id,
                date=date_val,
            )
            serializer = AttendanceSerializer(attendance, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(
                    {"message": "Attendance updated successfully", "data": serializer.data},
                    status=status.HTTP_200_OK,
                )
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        except Attendance.DoesNotExist:
            serializer = AttendanceSerializer(data=request.data)
            if serializer.is_valid():
                serializer.save()
                return Response(
                    {"message": "Attendance created successfully", "data": serializer.data},
                    status=status.HTTP_201_CREATED,
                )
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SalaryReportViewSet(
    RootAdminThrottleExemptMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    Read-only access to the persisted SalaryReport table.
    """
    serializer_class = SalaryReportSerializer
    permission_classes = [CanViewSalaryReport]

    def get_queryset(self):
        user = self.request.user
        qs = SalaryReport.objects.all() if is_admin_or_manager(user) else SalaryReport.objects.filter(user=user)

        user_id = self.request.query_params.get("user_id")
        if user_id:
            qs = qs.filter(user_id=user_id)

        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")
        if start_date:
            qs = qs.filter(start_date__gte=start_date)
        if end_date:
            qs = qs.filter(end_date__lte=end_date)

        is_finalized = self.request.query_params.get("is_finalized")
        if is_finalized is not None:
            qs = qs.filter(is_finalized=is_finalized.lower() in ("1", "true", "yes"))

        return qs.select_related("user").order_by("-start_date")

    @action(detail=False, methods=["post"])
    def refresh(self, request):
        user_id = request.data.get("user_id")
        if not user_id:
            return Response({"error": "user_id is required"}, status=400)

        if not (is_admin_or_manager(request.user) or str(request.user.id) == str(user_id)):
            return Response({"detail": "You do not have permission to refresh this user's reports."}, status=403)

        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return Response({"error": "user_id must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

        if not User.objects.filter(pk=user_id).exists():
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)

        queue_salary_report_refresh(user_id, countdown=0)
        return Response({"detail": f"Refresh queued for user {user_id}."})

    @action(detail=True, methods=["post"])
    def finalize(self, request, pk=None):
        report = self.get_object()
        if not is_admin_or_manager(request.user):
            return Response({"detail": "Only an admin can finalize a report."}, status=403)

        if report.is_finalized:
            return Response({"detail": "Already finalized."})

        report.is_finalized = True
        report.save(update_fields=["is_finalized", "updated_at"])
        return Response(SalaryReportSerializer(report).data)


class SalaryTransactionViewSet(RootAdminThrottleExemptMixin, viewsets.ModelViewSet):
    search_fields = [
        "user__first_name",
        "user__last_name",
        "user__employee_profile__employee_id",
        "user__employee_profile__vendor_name",
        "=user__email",
        "=user__mobile_number",
    ]

    filterset_fields = {
        "salary_report": ["exact"],
        "user": ["exact"],
        "status": ["exact", "in"],
        "payment_method": ["exact", "in"],
        "transaction_id": ["exact"],
        "payment_reference": ["exact"],
        "amount_paid": ["exact", "gte", "lte"],
        "paid_at": ["exact", "gte", "lte", "date", "isnull"],
        "processed_at": ["exact", "gte", "lte", "isnull"],
        "created_at": ["gte", "lte", "date"],
        "salary_report__start_date": ["exact"],
        "salary_report__end_date": ["exact"],
    }

    ordering_fields = [
        "created_at",
        "updated_at",
        "paid_at",
        "processed_at",
        "amount_paid",
        "status",
        "payment_method",
        "transaction_id",
        "payment_reference",
        "user__first_name",
        "user__last_name",
        "salary_report__start_date",
        "salary_report__end_date",
        "structure_basic",
        "structure_pf",
        "structure_esi",
        "structure_final",
    ]
    serializer_class = SalaryTransactionSerializer
    permission_classes = [IsPayrollAdminOrSelfReadOnly]

    def get_queryset(self):
        user = self.request.user
        queryset = SalaryTransaction.objects.select_related("user", "salary_report")

        if not is_admin_or_manager(user):
            queryset = queryset.filter(user=user)

        latest_structure = SalaryStructure.objects.filter(
            user=OuterRef("user_id"),
            effective_from__lte=OuterRef("salary_report__start_date"),
        ).order_by("-effective_from")

        queryset = queryset.annotate(
            structure_basic=Subquery(latest_structure.values("amount")[:1]),
            structure_pf=Subquery(latest_structure.values("pf_amount")[:1]),
            structure_esi=Subquery(latest_structure.values("esi_amount")[:1]),
            structure_final=Subquery(latest_structure.values("final_salary")[:1]),
        )

        return queryset

    def get_serializer_class(self):
        if self.action == "create":
            return SalaryTransactionCreateSerializer
        return SalaryTransactionSerializer

    @db_transaction.atomic
    def create(self, request, *args, **kwargs):
        user = request.user
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        salary_report = SalaryReport.objects.select_related("user").select_for_update().get(
            id=serializer.validated_data["salary_report_id"]
        )

        if not is_admin_or_manager(user):
            return Response(
                {"detail": "You do not have permission to record payments for this report."},
                status=status.HTTP_403_FORBIDDEN,
            )

        amount_paid = serializer.validated_data["amount_paid"]

        SalaryTransaction.objects.filter(
            salary_report=salary_report,
            status__in=["PENDING", "PROCESSING"],
        ).update(status="CANCELLED", processed_at=timezone.localtime())

        SalaryTransaction.objects.create(
            salary_report=salary_report,
            user=salary_report.user,
            amount_paid=amount_paid,
            payment_method=serializer.validated_data["payment_method"],
            payment_reference=serializer.validated_data.get("payment_reference", ""),
            note=serializer.validated_data.get("note", ""),
            processed_at=timezone.localtime(),
            status="SUCCESS",
        )

        paid_amount = SalaryTransaction.objects.filter(
            salary_report=salary_report,
            status="SUCCESS",
        ).aggregate(total=Sum("amount_paid"))["total"] or Decimal("0.00")

        salary_report.paid_amount = paid_amount
        if paid_amount >= salary_report.total_payable_amount:
            salary_report.remaining_payment = Decimal("0.00")
            salary_report.advance_amount = paid_amount - salary_report.total_payable_amount
        else:
            salary_report.remaining_payment = salary_report.total_payable_amount - paid_amount
            salary_report.advance_amount = Decimal("0.00")

        salary_report.save(
            update_fields=["paid_amount", "remaining_payment", "advance_amount", "updated_at"]
        )

        return Response(
            {"detail": "Payment paid successfully."},
            status=status.HTTP_200_OK,
        )
