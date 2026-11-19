from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.billing.models import Invoice, Payment

from .models import Wallet, WalletTransaction
from .serializers import (
    AutoPaySerializer,
    ExcessPaymentSerializer,
    PaymentSummarySerializer,
    RefundSerializer,
    WalletCashComboSerializer,
    WalletCreditSerializer,
    WalletDebitSerializer,
    WalletSerializer,
    WalletTransactionSerializer,
)
from apps.accounts.permissions import is_root_user, is_superadmin
from .services import WalletPaymentService


def is_admin_or_manager(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff or is_superadmin(user) or is_root_user(user):
        return True
    return user.groups.filter(name__in=["Tenant Admin", "Super User", "Branch Manager"]).exists()


class WalletViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for wallet operations.

    Endpoints:
    - GET /wallet/ - List (admin only)
    - GET /wallet/{id}/ - Retrieve wallet
    - GET /wallet/my-wallet/ - Get current user's wallet
    - GET /wallet/{id}/history/ - Transaction history
    - POST /wallet/{id}/debit/ - Debit from wallet
    - POST /wallet/{id}/credit/ - Credit to wallet
    - POST /wallet/{id}/auto-pay/ - Auto-pay for invoice
    - POST /wallet/{id}/excess-payment/ - Handle excess payment
    - POST /wallet/{id}/wallet-cash-combo/ - Apply wallet + cash
    - POST /wallet/{id}/refund/ - Refund to wallet
    - GET /wallet/{id}/summary/ - Payment summary
    """

    serializer_class = WalletSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Users can only see their own wallet, admins can see all"""
        if is_admin_or_manager(self.request.user):
            return Wallet.objects.all().select_related("user")
        return Wallet.objects.filter(user=self.request.user).select_related("user")

    def get_object(self):
        """Allow access to own wallet via 'my-wallet' or explicit ID"""
        if self.kwargs.get("pk") == "my-wallet":
            wallet, _ = Wallet.objects.get_or_create(user=self.request.user)
            return wallet
        return super().get_object()

    @action(detail=False, methods=["get"], url_path="my-wallet")
    def my_wallet(self, request):
        wallet, _ = Wallet.objects.get_or_create(user=request.user)
        serializer = self.get_serializer(wallet)
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        """Get transaction history for wallet"""
        wallet = self.get_object()
        limit = request.query_params.get("limit", 50)

        transactions = wallet.get_transaction_history(limit=int(limit))
        serializer = WalletTransactionSerializer(transactions, many=True)

        return Response({
            "wallet_id": wallet.id,
            "transaction_count": len(transactions),
            "transactions": serializer.data,
        })

    @action(detail=True, methods=["post"])
    def debit(self, request, pk=None):
        """Debit from wallet (manual operation)"""
        wallet = self.get_object()
        serializer = WalletDebitSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            txn = wallet.debit(
                amount=serializer.validated_data["amount"],
                source_type=WalletTransaction.SourceType.ADJUSTMENT,
                reference_id=serializer.validated_data.get("reference_id"),
                description=serializer.validated_data.get("description"),
            )

            return Response({
                "success": True,
                "transaction_id": txn.transaction_id,
                "amount": txn.amount,
                "new_balance": wallet.balance,
                "message": "Debit successful",
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({
                "success": False,
                "error": str(e),
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"])
    def credit(self, request, pk=None):
        """Credit to wallet (manual operation)"""
        wallet = self.get_object()
        serializer = WalletCreditSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            txn = wallet.credit(
                amount=serializer.validated_data["amount"],
                source_type=WalletTransaction.SourceType.ADJUSTMENT,
                reference_id=serializer.validated_data.get("reference_id"),
                description=serializer.validated_data.get("description"),
            )

            return Response({
                "success": True,
                "transaction_id": txn.transaction_id,
                "amount": txn.amount,
                "new_balance": wallet.balance,
                "message": "Credit successful",
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({
                "success": False,
                "error": str(e),
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="auto-pay")
    def auto_pay(self, request, pk=None):
        """Process auto-pay for an invoice"""
        wallet = self.get_object()
        serializer = AutoPaySerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        invoice_id = serializer.validated_data["invoice_id"]
        invoice = get_object_or_404(Invoice, id=invoice_id, user=wallet.user)

        order = invoice.secondary_order or invoice.ternary_order
        if not order and invoice.secondary_order_id:
            order = invoice.secondary_order

        try:
            result = WalletPaymentService.process_order_auto_pay(order, invoice)
            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                "success": False,
                "error": str(e),
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="excess-payment")
    def excess_payment(self, request, pk=None):
        """Handle excess payment"""
        wallet = self.get_object()
        serializer = ExcessPaymentSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        payment_id = serializer.validated_data["payment_id"]
        payment = get_object_or_404(Payment, id=payment_id)
        invoice = payment.invoice

        try:
            result = WalletPaymentService.handle_excess_payment(payment, invoice)
            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                "success": False,
                "error": str(e),
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="wallet-cash-combo")
    def wallet_cash_combo(self, request, pk=None):
        """Apply wallet + cash combo to invoice"""
        wallet = self.get_object()
        serializer = WalletCashComboSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        invoice_id = serializer.validated_data["invoice_id"]
        invoice = get_object_or_404(Invoice, id=invoice_id, user=wallet.user)

        try:
            result = WalletPaymentService.apply_wallet_and_cash(
                invoice,
                serializer.validated_data["cash_amount"],
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({
                "success": False,
                "error": str(e),
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"])
    def refund(self, request, pk=None):
        """Refund to wallet"""
        wallet = self.get_object()
        serializer = RefundSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        invoice_id = serializer.validated_data["invoice_id"]
        invoice = get_object_or_404(Invoice, id=invoice_id, user=wallet.user)

        try:
            result = WalletPaymentService.refund_to_wallet(
                invoice,
                serializer.validated_data["refund_amount"],
                serializer.validated_data.get("reason", ""),
            )
            return Response(result, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({
                "success": False,
                "error": str(e),
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get"])
    def summary(self, request, pk=None):
        """Get payment summary"""
        wallet = self.get_object()

        summary = WalletPaymentService.get_payment_summary(wallet.user)
        serializer = PaymentSummarySerializer(summary)

        return Response(serializer.data, status=status.HTTP_200_OK)
