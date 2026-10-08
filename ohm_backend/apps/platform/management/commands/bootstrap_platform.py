from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.accounts.models import User
from apps.platform.models import Tenant, Domain, PlatformAdmin
# python manage.py bootstrap_platform --email admin@ohm.com --password admin

class Command(BaseCommand):
    help = "Create the public (control-plane) tenant, its root hosts, and a platform superuser. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)

    def handle(self, *args, **opts):
        public = get_public_schema_name()
        hosts = list(dict.fromkeys([*settings.ROOT_HOSTS, settings.PLATFORM_HOST, *settings.API_HOSTS]))
        with schema_context(public):
            tenant, _ = Tenant.objects.get_or_create(
                schema_name=public, defaults={"name": "O-hm platform", "status": Tenant.Status.ACTIVE}
            )
            for i, host in enumerate(hosts):
                Domain.objects.get_or_create(
                    domain=host,
                    defaults={"tenant": tenant, "is_primary": i == 0, "verified": True, "verified_at": timezone.now()},
                )
            user = User.objects.filter(email=opts["email"].lower()).first()
            if user is None:
                user = User.objects.create_superuser(email=opts["email"], password=opts["password"])
            PlatformAdmin.objects.get_or_create(user=user)
        self.stdout.write(self.style.SUCCESS("Platform ready on: " + ", ".join(hosts)))
