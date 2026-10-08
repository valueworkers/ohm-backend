from django.conf import settings
from django.core.management.base import BaseCommand

from apps.platform.services import provision_approved_request


class Command(BaseCommand):
    help = "Provision a tenant only from an approved onboarding request."

    def add_arguments(self, parser):
        parser.add_argument("request_id", help="UUID of an approved onboarding request")

    def handle(self, *args, **options):
        onboarding_request = provision_approved_request(options["request_id"])
        tenant = onboarding_request.tenant
        self.stdout.write(self.style.SUCCESS(f"Tenant {tenant.name} provisioned with schema {tenant.schema_name}."))
        custom = tenant.domains.filter(domain=onboarding_request.custom_domain, verified=False).first()
        if custom:
            self.stdout.write(
                f"\nCustom domain {custom.domain} (pending verification):\n"
                f"  CNAME  {custom.domain} -> {settings.EDGE_HOST}\n"
                f"  TXT    {settings.DOMAIN_VERIFY_PREFIX}.{custom.domain} = {custom.verification_token}\n"
            )
