from django.apps import apps
from django.db.models.signals import post_migrate
from django.dispatch import receiver
from django_tenants.utils import get_public_schema_name


@receiver(post_migrate, dispatch_uid="platform.create_app_features")
def create_features_for_installed_apps(sender, using, **kwargs):
    """Ensure installed OHM product apps are available in the feature catalog."""
    from .models import Feature

    connection = kwargs.get("connection")
    if not connection or getattr(connection, "schema_name", None) != get_public_schema_name():
        return

    for app_config in apps.get_app_configs():
        if not app_config.name.startswith("apps.") or app_config.label == "tenants":
            continue

        Feature.objects.using(using).get_or_create(
            code=app_config.label,
            defaults={"name": str(app_config.verbose_name)},
        )
