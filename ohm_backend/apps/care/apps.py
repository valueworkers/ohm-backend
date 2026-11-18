from django.apps import AppConfig


class CareConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.care"
    label = "care"
    verbose_name = "Care Product Module"

    def ready(self):
        try:
            import apps.care.signals  # noqa: F401
        except ImportError:
            pass
