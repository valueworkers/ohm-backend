from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.wallet.models import Wallet


class Command(BaseCommand):
    help = "Create wallets for all users who don't have one"

    def add_arguments(self, parser):
        parser.add_argument(
            "--customers-only",
            action="store_true",
            help="Only create wallets for customer users",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        user_model = get_user_model()
        if options["customers_only"] and hasattr(user_model.objects, "customers"):
            users = user_model.objects.customers().exclude(wallet__isnull=False)
        else:
            users = user_model.objects.all().exclude(wallet__isnull=False)

        created_count = 0
        for user in users:
            Wallet.objects.create(user=user)
            created_count += 1
            self.stdout.write(
                self.style.SUCCESS(f"Wallet created for: {user}")
            )

        self.stdout.write(
            self.style.SUCCESS(f"\nSuccessfully created {created_count} wallets.")
        )
