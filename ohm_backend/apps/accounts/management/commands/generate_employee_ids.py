from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

from apps.accounts.models import User, EmployeeProfile


class Command(BaseCommand):
    help = "Generate employee IDs for existing EmployeeProfiles in a tenant schema"

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema",
            required=True,
            help="Tenant schema name, e.g. vaishnavi-medicare",
        )

    def handle(self, *args, **options):
        schema = options["schema"]

        # 1. Select the schema first: validate that it exists.
        TenantModel = get_tenant_model()
        if not TenantModel.objects.filter(schema_name=schema).exists():
            raise CommandError(f"Tenant schema '{schema}' does not exist.")

        self.stdout.write(f"Using schema: {schema}")

        # 2. Everything below runs inside that schema.
        with schema_context(schema):
            self._generate_ids()

    def _generate_ids(self):
        prefix_map = {
            "Manager": "M",
            "Staff": "S",
        }

        year = timezone.now().year
        generated_count = 0
        skipped_count = 0

        users = list(User.objects.employees().order_by("id"))

        for user in users:
            profile, _ = EmployeeProfile.objects.get_or_create(user=user)

            # `or ""` guards against employee_id being NULL
            # if len(profile.employee_id or "") > 2:
            #     skipped_count += 1
            #     continue

            prefix = prefix_map.get(user.primary_role)
            if not prefix:
                skipped_count += 1
                continue

            employee_id = f"{prefix}{year}{user.id:04d}"

            updated = (
                EmployeeProfile.objects
                .filter(pk=profile.pk)
                .update(employee_id=employee_id)
            )

            if updated:
                generated_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Generated: {employee_id} for "
                        f"{user.get_full_name()} (User ID: {user.id})"
                    )
                )
            else:
                skipped_count += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Employee IDs generated: {generated_count}"))
        self.stdout.write(self.style.WARNING(f"Profiles skipped: {skipped_count}"))