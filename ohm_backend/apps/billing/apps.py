from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "apps.billing"
    label = "billing"
    verbose_name = "Billing"

    def ready(self):
        import apps.billing.signals  # noqa: F401 – connect receivers
