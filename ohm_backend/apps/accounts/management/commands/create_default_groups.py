from django.core.management.base import BaseCommand
from django_tenants.utils import get_public_schema_name, schema_context

from apps.accounts.services import (
    seed_roles_and_groups_for_schema,
    sync_public_schema_roles_and_permissions,
    sync_tenant_roles_and_permissions,
)
from apps.platform.models import Tenant


class Command(BaseCommand):
    help = "Create default groups, roles, and assign permissions automatically to existing tenants, owners, and users"

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

    def _configure_schema(self, schema_name, assign_users=True):
        is_public = schema_name == get_public_schema_name()
        self.stdout.write(f"\nConfiguring schema: {schema_name} (public={is_public})")

        if not assign_users:
            with schema_context(schema_name):
                seed_roles_and_groups_for_schema(is_public=is_public)
            self.stdout.write(self.style.SUCCESS(f"✅ Schema '{schema_name}' groups/roles seeded (users skipped)."))
            return

        if is_public:
            res = sync_public_schema_roles_and_permissions()
            self.stdout.write(f"  • Seeded default groups & roles")
            self.stdout.write(f"  • Superusers assigned: {len(res['superusers'])} ({', '.join(res['superusers']) or 'none'})")
            self.stdout.write(f"  • Staff users assigned: {len(res['staff_users'])} ({', '.join(res['staff_users']) or 'none'})")
            self.stdout.write(self.style.SUCCESS(f"✅ Schema '{schema_name}' configured successfully."))
        else:
            tenant = Tenant.objects.filter(schema_name=schema_name).first()
            if not tenant:
                self.stdout.write(self.style.ERROR(f"❌ Tenant for schema '{schema_name}' not found."))
                return
            res = sync_tenant_roles_and_permissions(tenant)
            self.stdout.write(f"  • Seeded default groups & roles")
            self.stdout.write(f"  • Owners mirrored & assigned TENANT_ADMIN: {len(res['assigned_owners'])} ({', '.join(res['assigned_owners']) or 'none'})")
            self.stdout.write(f"  • Tenant users synchronized: {len(res['synced_users'])}")
            self.stdout.write(self.style.SUCCESS(f"✅ Tenant '{tenant.name}' ({schema_name}) configured successfully."))

    def handle(self, *args, **options):
        target_schema = options.get("schema")
        assign_users = not options.get("no_users", False)
        public = get_public_schema_name()

        if target_schema:
            self._configure_schema(target_schema, assign_users=assign_users)
            return

        # 1. Public schema
        self._configure_schema(public, assign_users=assign_users)

        # 2. All tenant schemas
        tenants = Tenant.objects.exclude(schema_name=public)
        for tenant in tenants:
            try:
                self._configure_schema(tenant.schema_name, assign_users=assign_users)
            except Exception as exc:
                self.stdout.write(
                    self.style.ERROR(f"❌ Failed to configure tenant '{tenant.schema_name}': {exc}")
                )

        self.stdout.write(self.style.SUCCESS("\n🎉 All schemas, roles, permissions, and tenant owners configured successfully!"))

