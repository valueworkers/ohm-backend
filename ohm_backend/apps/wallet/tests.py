from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django_tenants.test.cases import TenantTestCase

from .models import Wallet, WalletTransaction

User = get_user_model()


class WalletModelTests(TenantTestCase):
    @classmethod
    def setup_tenant(cls, tenant):
        tenant.name = "Wallet Provider"
        tenant.status = "active"

    def setUp(self):
        super().setUp()
        self.user1 = User.objects.create_user(email="u1@test.com", password="pwd")
        self.user2 = User.objects.create_user(email="u2@test.com", password="pwd")
        self.wallet1, _ = Wallet.objects.get_or_create(user=self.user1)
        self.wallet2, _ = Wallet.objects.get_or_create(user=self.user2)

    def test_credit_debit(self):
        self.wallet1.credit(Decimal("100.00"), source_type=WalletTransaction.SourceType.ADJUSTMENT)
        self.assertEqual(self.wallet1.balance, Decimal("100.00"))

        self.wallet1.debit(Decimal("40.00"), source_type=WalletTransaction.SourceType.ORDER_PAYMENT)
        self.assertEqual(self.wallet1.balance, Decimal("60.00"))

    def test_insufficient_balance(self):
        with self.assertRaises(ValidationError):
            self.wallet1.debit(Decimal("50.00"), source_type=WalletTransaction.SourceType.ORDER_PAYMENT)

    def test_transfer_to(self):
        self.wallet1.credit(Decimal("100.00"), source_type=WalletTransaction.SourceType.ADJUSTMENT)
        self.wallet1.transfer_to(self.user2, Decimal("30.00"))
        self.wallet1.refresh_from_db()
        self.wallet2.refresh_from_db()
        self.assertEqual(self.wallet1.balance, Decimal("70.00"))
        self.assertEqual(self.wallet2.balance, Decimal("30.00"))
