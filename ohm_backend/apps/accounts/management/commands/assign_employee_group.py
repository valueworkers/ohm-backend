import re

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_tenant_model, schema_context

from apps.accounts.models import EmployeeProfile

# employee_id prefix -> group name
PREFIX_GROUP_MAP = {
    "S": "Staff",
    "M": "Manager",
}


class Command(BaseCommand):
    help = "Assign groups to employees based on their employee ID prefix"

    def add_arguments(self, parser):
        parser.add_argument("--schema", required=True, help="Tenant schema name")

    def handle(self, *args, **options):
        schema = options["schema"]

        # 1. Select the schema first
        if not get_tenant_model().objects.filter(schema_name=schema).exists():
            raise CommandError(f"Tenant schema '{schema}' does not exist.")

        self.stdout.write(f"Using schema: {schema}")

        with schema_context(schema):
            self._assign_groups()

    def _assign_groups(self):
        # 2. Get or create all groups up front (inside the tenant schema)
        groups = {}
        for prefix, name in PREFIX_GROUP_MAP.items():
            group, created = Group.objects.get_or_create(name=name)
            groups[prefix] = group
            if created:
                self.stdout.write(self.style.WARNING(f"Created group '{name}'"))

        assigned_count = 0
        skipped_count = 0

        profiles = (
            EmployeeProfile.objects
            .select_related("user")
            .order_by("user_id")
        )

        for profile in profiles:
            user = profile.user
            employee_id = profile.employee_id or ""

            # Leading letters of the ID, e.g. "S20260012" -> "S", "LM20260005" -> "LM"
            match = re.match(r"^[A-Za-z]+", employee_id)
            prefix = match.group(0).upper() if match else None

            group = groups.get(prefix)
            if not group:
                self.stdout.write(
                    f"Skipped: {user.get_full_name()} (User ID: {user.id}), "
                    f"no group mapped for ID '{employee_id or 'empty'}'"
                )
                skipped_count += 1
                continue

            if user.groups.filter(pk=group.pk).exists():
                skipped_count += 1
                continue

            user.groups.add(group)
            assigned_count += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"Assigned '{group.name}' to {user.get_full_name()} "
                    f"({employee_id}, User ID: {user.id})"
                )
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Users assigned: {assigned_count}"))
        self.stdout.write(self.style.WARNING(f"Skipped: {skipped_count}"))