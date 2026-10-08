from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email, password, **extra):
        if not email:
            raise ValueError("Email is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra["is_staff"] = True
        extra["is_superuser"] = True
        return self._create(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """One table per schema: platform staff in public, tenant users in each tenant schema."""

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.email

    @property
    def primary_role(self):
        """Highest-ranked tenant-wide role code (None for platform users such as the Super Admin)."""
        best = None
        for a in self.role_assignments.all():
            if a.scope_type_id is None and (best is None or a.role.rank > best.rank):
                best = a.role
        return best.code if best else None


class Role(models.Model):
    class Code(models.TextChoices):
        TENANT_ADMIN = "TENANT_ADMIN", "Tenant Admin"
        CUSTOMER = "CUSTOMER", "Customer / User"
        # Reserved for later: sub-roles that will sit under Tenant Admin (not seeded yet)
        VENUE_ADMIN = "VENUE_ADMIN", "Venue Admin"
        SERVICE_ADMIN = "SERVICE_ADMIN", "Service Admin"
        RESOURCE_ADMIN = "RESOURCE_ADMIN", "Resource Admin"
        OPS_MANAGER = "OPS_MANAGER", "Ops Manager"
        STAFF = "STAFF", "Staff Member"

    code = models.CharField(max_length=30, unique=True, choices=Code.choices)
    name = models.CharField(max_length=60)
    rank = models.PositiveSmallIntegerField(help_text="Higher = more authority")
    permissions = models.ManyToManyField("accounts.Permission", blank=True, related_name="roles")

    def __str__(self):
        return self.name


class Permission(models.Model):
    """Product-level permission code assignable to one or more roles."""

    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.code


class EmployeeProfile(models.Model):
    """Employee details for a user inside the current tenant schema."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="employee_profile")
    employee_id = models.CharField(max_length=80, blank=True)
    job_title = models.CharField(max_length=120, blank=True)
    department = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)
    hired_at = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.user.email


class RoleAssignment(models.Model):
    """Grants a role either tenant-wide (scope null) or on one object (Venue/Service/Resource)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="role_assignments")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="assignments")
    scope_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.CASCADE)
    scope_id = models.PositiveBigIntegerField(null=True, blank=True)
    scope = GenericForeignKey("scope_type", "scope_id")
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role", "scope_type", "scope_id"],
                nulls_distinct=False,
                name="uniq_user_role_scope",
            )
        ]
