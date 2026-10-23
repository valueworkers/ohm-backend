from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.accounts.models import User
from apps.accounts.services import assign_all_permissions_to_superuser
from apps.platform.models import Domain, PlatformAdmin, Tenant

# python manage.py bootstrap_platform --email admin@ohm.com --password admin --full-name "OHM Admin" --phone "+919999999999" --title "Super Admin"

DEFAULT_HOSTS = ["o-hm.com", "admin.o-hm.com", "api.o-hm.com"]


class Command(BaseCommand):
    help = (
        "Create the public (control-plane) tenant, its root hosts, and a platform "
        "superuser with a complete profile. Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--full-name", default=None)
        parser.add_argument("--phone", default=None)
        parser.add_argument("--title", default=None, help="PlatformAdmin title, e.g. 'Super Admin'")
        parser.add_argument(
            "--reset-password",
            action="store_true",
            help="Set the password even if the user already exists.",
        )

    def _hosts(self):
        configured = [
            *getattr(settings, "ROOT_HOSTS", []),
            getattr(settings, "PLATFORM_HOST", None),
            *getattr(settings, "API_HOSTS", []),
        ]
        hosts = [h.strip().lower() for h in [*DEFAULT_HOSTS, *configured] if h]
        return list(dict.fromkeys(hosts))  # de-dupe, keep order (o-hm.com first)

    @transaction.atomic
    def handle(self, *args, **opts):
        public = get_public_schema_name()
        email = opts["email"].strip().lower()
        hosts = self._hosts()
        now = timezone.now()

        with schema_context(public):
            # --- Public tenant ---
            tenant, _ = Tenant.objects.get_or_create(
                schema_name=public,
                defaults={
                    "name": "O-hm platform",
                    "status": Tenant.Status.ACTIVE,
                    "contact_email": email,
                },
            )
            if tenant.status != Tenant.Status.ACTIVE:
                tenant.status = Tenant.Status.ACTIVE
                tenant.save(update_fields=["status"])

            # --- Domains (all verified, exactly one primary) ---
            has_primary = Domain.objects.filter(tenant=tenant, is_primary=True).exists()
            for host in hosts:
                domain, created = Domain.objects.get_or_create(
                    domain=host,
                    defaults={
                        "tenant": tenant,
                        "is_primary": not has_primary,
                        "verified": True,
                        "verified_at": now,
                    },
                )
                if created:
                    has_primary = True
                    continue
                if domain.tenant_id != tenant.pk:
                    self.stdout.write(self.style.WARNING(
                        f"{host} already belongs to another tenant; skipped."
                    ))
                    continue
                if not domain.verified:
                    domain.verified = True
                    domain.verified_at = now
                    domain.save(update_fields=["verified", "verified_at"])

            # --- Superuser with full profile ---
            user = User.objects.filter(email=email).first()
            created_user = user is None
            if created_user:
                user = User.objects.create_superuser(
                    email=email,
                    password=opts["password"],
                    full_name=opts["full_name"] or "Platform Admin",
                    phone=opts["phone"] or "",
                )
            else:
                changed = []
                if opts["full_name"] is not None:
                    user.full_name = opts["full_name"]; changed.append("full_name")
                if opts["phone"] is not None:
                    user.phone = opts["phone"]; changed.append("phone")
                for flag in ("is_active", "is_staff", "is_superuser"):
                    if not getattr(user, flag):
                        setattr(user, flag, True); changed.append(flag)
                if opts["reset_password"]:
                    user.set_password(opts["password"]); changed.append("password")
                if changed:
                    user.save()

            # --- Platform admin profile ---
            profile, _ = PlatformAdmin.objects.get_or_create(
                user=user, defaults={"title": opts["title"] or "Super Admin"}
            )
            if opts["title"] and profile.title != opts["title"]:
                profile.title = opts["title"]
                profile.save(update_fields=["title"])

            assign_all_permissions_to_superuser(user)

        self.stdout.write(self.style.SUCCESS(
            f"Platform ready on: {', '.join(hosts)}\n"
            f"Admin: {user.email} ({'created' if created_user else 'existing'}), "
            f"title: {profile.title}"
        ))