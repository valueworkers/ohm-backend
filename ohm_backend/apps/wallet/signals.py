from django.conf import settings
from django.db import connection
from django.db.models.signals import post_save
from django.dispatch import receiver
from django_tenants.utils import get_public_schema_name

from .models import Wallet


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_wallet_for_user(sender, instance, created, **kwargs):
    """Auto-create wallet when an account is created in a tenant schema."""
    if kwargs.get("raw", False):
        return
    if connection.schema_name == get_public_schema_name():
        return
    if created:
        Wallet.objects.get_or_create(user=instance)

