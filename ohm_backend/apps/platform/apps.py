from django.apps import AppConfig


class PlatformConfig(AppConfig):
    name = "apps.platform"
    label = "tenants"

    def ready(self):
        import apps.platform.signals  # noqa: F401 – connect receivers
