import logging
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# Care product signals. Core PrimaryOrder/SecondaryOrder/TernaryOrder/Payment totals
# and invoice synchronizations are handled by `apps.billing.signals`.
