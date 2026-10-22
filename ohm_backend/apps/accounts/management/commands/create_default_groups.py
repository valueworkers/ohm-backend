from django.core.management.base import BaseCommand
from django_tenants.utils import get_public_schema_name, schema_context

from apps.accounts.services import (
    assign_roles_and_permissions_to_existing_tenants_and_owners,
    seed_roles_and_groups_for_schema,
)
from apps.platform.models import Tenant


class Command(BaseCommand):
    help = "Create role groups and optionally synchronize users and tenant owners"

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema",
            "--tenant",
            dest="schema",
            help="Specific schema to configure (defaults to public and all tenant schemas).",
        )
        parser.add_argument(
            "--no-users",
            action="store_true",
            help="Only seed groups/roles and permissions; do not assign roles/permissions to users or owners.",
        )

    def _configure_schema(self, schema_name):
        is_public = schema_name == get_public_schema_name()
        self.stdout.write(f"\nConfiguring schema: {schema_name} (public={is_public})")
        with schema_context(schema_name):
            seed_roles_and_groups_for_schema(is_public=is_public)
        self.stdout.write(self.style.SUCCESS(f"✅ Schema '{schema_name}' groups/roles seeded (users skipped)."))

    def handle(self, *args, **options):
        target_schema = options.get("schema")
        assign_users = not options.get("no_users", False)
        public = get_public_schema_name()

        if assign_users:
            results = assign_roles_and_permissions_to_existing_tenants_and_owners(target_schema=target_schema)
            for schema_name, result in results.items():
                if schema_name == public:
                    self.stdout.write(f"\nConfiguring schema: {schema_name} (public=True)")
                    self.stdout.write(f"  • Superusers assigned: {len(result['superusers'])} ({', '.join(result['superusers']) or 'none'})")
                    self.stdout.write(f"  • Staff users assigned: {len(result['staff_users'])} ({', '.join(result['staff_users']) or 'none'})")
                else:
                    self.stdout.write(f"\nConfiguring tenant schema: {schema_name}")
                    self.stdout.write(f"  • Owners mirrored & assigned TENANT_ADMIN: {len(result['assigned_owners'])} ({', '.join(result['assigned_owners']) or 'none'})")
                    self.stdout.write(f"  • Tenant users synchronized: {len(result['synced_users'])}")
                self.stdout.write(self.style.SUCCESS(f"✅ Schema '{schema_name}' configured successfully."))
            self.stdout.write(self.style.SUCCESS("\n🎉 All schemas, groups, and tenant owners configured successfully!"))
            return

        if target_schema:
            self._configure_schema(target_schema)
            return

        # 1. Public schema
        self._configure_schema(public)

        # 2. All tenant schemas
        tenants = Tenant.objects.exclude(schema_name=public)
        for tenant in tenants:
            try:
                self._configure_schema(tenant.schema_name)
            except Exception as exc:
                self.stdout.write(
                    self.style.ERROR(f"❌ Failed to configure tenant '{tenant.schema_name}': {exc}")
                )

        self.stdout.write(self.style.SUCCESS("\n🎉 All schemas, roles, permissions, and tenant owners configured successfully!"))

