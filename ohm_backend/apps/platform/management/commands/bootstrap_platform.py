"""
apps/platform/management/commands/bootstrap_platform.py

One-shot initial setup for the O-hm platform (idempotent, safe to re-run):
  1. Public (control-plane) tenant + root domains
  2. Groups & Roles: "Super User" (all perms), "Tenant Admin" (see TENANT_ADMIN rules)
  3. Default Trial plan
  4. Super Admin user + PlatformAdmin profile
  5. Super User group + SUPER_ADMIN role assigned to the Super Admin

python manage.py bootstrap_platform --email admin@ohm.com --password admin --first-name "Pankaj" --last-name "shinde" --mobile-number "9518972842"

Optional: also seed groups/roles inside tenant schemas
  --all-tenants            every existing tenant schema
  --schema acme            specific tenant schema (repeatable)
Run after `migrate_schemas` (Permission rows are created on post_migrate).
"""
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

from apps.accounts.models import User, Role, RoleAssignment
from apps.accounts.services import AccountsService
from apps.platform.models import Domain, PlatformAdmin, Plan, Tenant

DEFAULT_HOSTS = ["o-hm.com", "admin.o-hm.com", "api.o-hm.com"]

SYSTEM_APPS = ["admin", "auth", "contenttypes", "sessions"]

# Each scope is a list of rules; a role gets the UNION of its rules. {} = every permission.
# Rule keys: apps, models, exclude_apps, prefixes.
ROLE_SPECS = [
    {
        "code": Role.Code.SUPER_ADMIN,
        "role_name": "Super Admin",
        "group": "Super User",
        "rank": 100,
        "scopes": {"public": [{}], "tenant": [{}]},
    },
    {
        "code": Role.Code.TENANT_ADMIN,
        "role_name": "Tenant Admin",
        "group": "Tenant Admin",
        "rank": 80,
        "scopes": {
            # Public schema: read platform data + view/change User.
            # "Own data only" must be enforced in the accounts API (filter to request.user).
            "public": [
                {"apps": ["platform"], "prefixes": ["view_"]},
                {"apps": ["accounts"], "models": ["user"], "prefixes": ["view_", "change_"]},
            ],
            # Tenant schema: everything in his own schema.
            "tenant": [{"exclude_apps": SYSTEM_APPS + ["platform"]}],
        },
    },
    # The account app uses these built-in roles for its tenant workflows. Keep
    # their empty permission groups in the bootstrap catalogue so runtime code
    # can assign them without creating a second set of groups.
    {
        "code": Role.Code.CUSTOMER,
        "role_name": "Customer / User",
        "group": Role.Code.CUSTOMER,
        "rank": 10,
        "scopes": {"tenant": []},
    },
    {
        "code": Role.Code.OPS_MANAGER,
        "role_name": "Ops Manager",
        "group": Role.Code.OPS_MANAGER,
        "rank": 80,
        "scopes": {"public": [], "tenant": []},
    },
    {
        "code": Role.Code.VENUE_ADMIN,
        "role_name": "Venue Admin",
        "group": Role.Code.VENUE_ADMIN,
        "rank": 70,
        "scopes": {"tenant": []},
    },
    {
        "code": Role.Code.SERVICE_ADMIN,
        "role_name": "Service Admin",
        "group": Role.Code.SERVICE_ADMIN,
        "rank": 70,
        "scopes": {"tenant": []},
    },
    {
        "code": Role.Code.RESOURCE_ADMIN,
        "role_name": "Resource Admin",
        "group": Role.Code.RESOURCE_ADMIN,
        "rank": 70,
        "scopes": {"tenant": []},
    },
    {
        "code": Role.Code.STAFF,
        "role_name": "Staff Member",
        "group": Role.Code.STAFF,
        "rank": 50,
        "scopes": {"public": [], "tenant": []},
    },
]

# Trial plan matches apps.platform.models.Plan (code, name, description, max_employees,
# max_branches, is_active). null limit = unlimited. Plan has no price/duration fields:
# the trial length lives on Subscription.trial_ends_at, so use TRIAL_DAYS when subscribing a tenant.
TRIAL_DAYS = 14
TRIAL_PLANS = [
    {
        "code": "trial",
        "name": "Trial",
        "description": f"Free {TRIAL_DAYS}-day trial for newly onboarded tenants.",
        "max_employees": 5,
        "max_branches": 1,
        "is_active": True,
    },
]


def permission_ids(rules):
    ids = set()
    for rule in rules:
        qs = Permission.objects.all()
        if rule.get("apps"):
            qs = qs.filter(content_type__app_label__in=rule["apps"])
        if rule.get("models"):
            qs = qs.filter(content_type__model__in=rule["models"])
        if rule.get("exclude_apps"):
            qs = qs.exclude(content_type__app_label__in=rule["exclude_apps"])
        if rule.get("prefixes"):
            q = Q()
            for p in rule["prefixes"]:
                q |= Q(codename__startswith=p)
            qs = qs.filter(q)
        ids.update(qs.values_list("pk", flat=True))
    return ids


class Command(BaseCommand):
    help = (
        "Initial platform setup: public tenant + domains, Super User / Tenant Admin "
        "groups & roles, Trial plan, Super Admin user with group + role. Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--first-name", default=None)
        parser.add_argument("--last-name", default=None)
        parser.add_argument("--mobile-number", default=None)
        parser.add_argument("--title", default="Super Admin")
        parser.add_argument("--reset-password", action="store_true",
                            help="Set the password even if the user already exists.")
        parser.add_argument("--all-tenants", action="store_true",
                            help="Also seed groups/roles in every tenant schema.")
        parser.add_argument("--schema", action="append", default=[],
                            help="Also seed groups/roles in this tenant schema (repeatable).")

    # ------------------------------------------------------------------ helpers
    def _hosts(self):
        configured = [
            *getattr(settings, "ROOT_HOSTS", []),
            getattr(settings, "PLATFORM_HOST", None),
            *getattr(settings, "API_HOSTS", []),
        ]
        hosts = [h.strip().lower() for h in [*DEFAULT_HOSTS, *configured] if h]
        return list(dict.fromkeys(hosts))  # de-dupe, keep order

    def _seed_roles(self, scope):
        """Create/sync Groups + Roles for the current schema ('public' or 'tenant' scope)."""
        for spec in ROLE_SPECS:
            rules = spec["scopes"].get(scope)
            if rules is None:
                continue
            perms = Permission.objects.filter(pk__in=permission_ids(rules))

            role, _ = Role.objects.update_or_create(
                code=spec["code"],
                defaults={"name": spec["role_name"], "rank": spec["rank"]},
            )
            role.permissions.set(perms)

            group, _ = Group.objects.get_or_create(name=spec["group"])
            group.permissions.set(perms)

            self.stdout.write(f"  [{scope}] {spec['group']}: {perms.count()} permissions")

    def _seed_plans(self):
        for data in TRIAL_PLANS:
            data = dict(data)
            plan, created = Plan.objects.update_or_create(
                code=data.pop("code"), defaults=data
            )
            self.stdout.write(f"  Plan '{plan}' {'created' if created else 'updated'}")

    # ------------------------------------------------------------------- main
    @transaction.atomic
    def handle(self, *args, **opts):
        public = get_public_schema_name()
        Tenant_ = get_tenant_model()
        email = opts["email"].strip().lower()
        hosts = self._hosts()
        now = timezone.now()

        extra_schemas = []
        if opts["all_tenants"]:
            extra_schemas += list(
                Tenant_.objects.exclude(schema_name=public).values_list("schema_name", flat=True)
            )
        for name in opts["schema"]:
            if name == public:
                continue
            if not Tenant_.objects.filter(schema_name=name).exists():
                raise CommandError(f"Unknown tenant schema: {name}")
            extra_schemas.append(name)

        with schema_context(public):
            # 1. Public tenant
            tenant, _ = Tenant.objects.get_or_create(
                schema_name=public,
                defaults={"name": "O-hm platform", "status": Tenant.Status.ACTIVE, "contact_email": email},
            )
            if tenant.status != Tenant.Status.ACTIVE:
                tenant.status = Tenant.Status.ACTIVE
                tenant.save(update_fields=["status"])

            # Domains (all verified, exactly one primary)
            has_primary = Domain.objects.filter(tenant=tenant, is_primary=True).exists()
            for host in hosts:
                domain, created = Domain.objects.get_or_create(
                    domain=host,
                    defaults={"tenant": tenant, "is_primary": not has_primary,
                              "verified": True, "verified_at": now},
                )
                if created:
                    has_primary = True
                    continue
                if domain.tenant_id != tenant.pk:
                    self.stdout.write(self.style.WARNING(f"{host} already belongs to another tenant; skipped."))
                    continue
                if not domain.verified:
                    domain.verified = True
                    domain.verified_at = now
                    domain.save(update_fields=["verified", "verified_at"])

            # 2. Groups & roles (public scope)
            self.stdout.write("Seeding groups & roles (public)...")
            self._seed_roles("public")

            # 3. Default Trial plan
            self.stdout.write("Seeding plans...")
            self._seed_plans()

            # 4. Super Admin user
            user = User.objects.filter(email=email).first()
            created_user = user is None
            if created_user:
                user = User.objects.create_superuser(
                    email=email,
                    password=opts["password"],
                    first_name=opts["first_name"] or "Platform Admin",
                    last_name=opts["last_name"] or "Platform Admin",
                    mobile_number=opts["mobile_number"] or None,
                )
            else:
                changed = []
                if opts["mobile_number"] is not None:
                    user.mobile_number = opts["mobile_number"] or None; changed.append("mobile_number")
                for flag in ("is_active", "is_staff", "is_superuser"):
                    if not getattr(user, flag):
                        setattr(user, flag, True); changed.append(flag)
                if opts["reset_password"]:
                    user.set_password(opts["password"]); changed.append("password")
                if changed:
                    user.save()

            profile, _ = PlatformAdmin.objects.get_or_create(
                user=user, defaults={"title": opts["title"] or "Super Admin"}
            )
            if opts["title"] and profile.title != opts["title"]:
                profile.title = opts["title"]
                profile.save(update_fields=["title"])

            # 5. Assign Super User group + SUPER_ADMIN role (platform-wide, null scope)
            user.groups.add(Group.objects.get(name="Super User"))
            super_role = Role.objects.get(code=Role.Code.SUPER_ADMIN)
            RoleAssignment.objects.get_or_create(
                user=user, role=super_role, scope_type=None, scope_id=None,
                defaults={"assigned_by": user},
            )
            AccountsService.assign_all_permissions_to_superuser(user)

        # Optional: groups & roles inside tenant schemas
        for schema in dict.fromkeys(extra_schemas):
            self.stdout.write(f"Seeding groups & roles ({schema})...")
            with schema_context(schema):
                self._seed_roles("tenant")

        self.stdout.write(self.style.SUCCESS(
            f"Platform ready on: {', '.join(hosts)}\n"
            f"Admin: {user.email} ({'created' if created_user else 'existing'}), "
            f"title: {profile.title}, group: Super User, role: {super_role.name}"
        ))
