# ohm_backend/apps/accounts/models.py
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models.functions import Lower
from django.utils import timezone

# ------------------------ USER MANAGER ------------------------
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
        user = self._create(email, password, **extra)
        from django.db import connection
        from django_tenants.utils import get_public_schema_name
        if connection.schema_name == get_public_schema_name() and user.is_staff and not user.is_superuser:
            from apps.accounts.services import AccountsService
            AccountsService.assign_default_permissions_to_public_root_user(user)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra["is_staff"] = True
        extra["is_superuser"] = True
        user = self._create(email, password, **extra)
        return user

    # ---------------- filters ----------------
    def active(self):
        return self.filter(is_deleted=False, is_active=True)

    def employees(self):
        """Users that have an employment record in this schema."""
        return self.filter(employee_profile__isnull=False)

    def customers(self):
        return self.with_role(Role.Code.CUSTOMER)

    def with_role(self, *role_codes):
        """Users holding any of these roles (tenant-wide or scoped)."""
        return self.filter(role_assignments__role__code__in=role_codes).distinct()


# ------------------------ USER ------------------------
class User(AbstractBaseUser, PermissionsMixin):
    """One table per schema: platform staff in public, tenant users in each tenant schema.

    Uses Django's own PermissionsMixin (is_superuser, groups, user_permissions) and
    auth.Permission / auth.Group. Role + RoleAssignment (below) add scoped role grants.
    """

    GENDER_CHOICES = [
        ("M", "Male"),
        ("F", "Female"),
        ("O", "Other"),
        ("N", "Prefer not to say"),
    ]

    phone_regex = RegexValidator(regex=r"^\d{10}$", message="Phone number must be 10 digits")

    # identity / login
    email = models.EmailField(unique=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    profile_pic = models.ImageField(upload_to="profile_photos/", null=True, blank=True)

    first_name = models.CharField(max_length=30, blank=True)
    middle_name = models.CharField(max_length=30, blank=True)
    last_name = models.CharField(max_length=30, blank=True)

    # contact (blank for platform staff, so unique+null)
    mobile_number = models.CharField(
        max_length=10, unique=True, null=True, blank=True, validators=[phone_regex]
    )
    alternate_phone_number = models.CharField(max_length=10, blank=True, validators=[phone_regex])
    emergency_contact_name = models.CharField(max_length=100, blank=True)
    emergency_contact_number = models.CharField(max_length=15, blank=True, validators=[phone_regex])

    # personal
    age = models.PositiveIntegerField(null=True, blank=True)
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, blank=True)
    permanent_address = models.TextField(blank=True)
    current_address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True, db_index=True)

    # status
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    created_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="created_users"
    )

    objects = UserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["is_deleted", "is_active"], name="accounts_us_is_del_9f42a1_idx")]
        constraints = [models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_uniq")]

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    # ---------------- helpers ----------------
    def get_full_name(self):
        return " ".join(p for p in (self.first_name, self.middle_name, self.last_name) if p)

    @property
    def primary_role(self):
        """Highest-ranked tenant-wide role name (None for platform users such as the Super Admin)."""
        best = None
        for a in self.role_assignments.all():
            if a.scope_type_id is None and (best is None or a.role.rank > best.rank):
                best = a.role
        return best.name if best else None

    @property
    def employee_id(self):
        profile = getattr(self, "employee_profile", None)
        return profile.employee_id if profile else None

    @property
    def is_owner(self):
        return self.is_superuser or self.role_assignments.filter(role__code=Role.Code.TENANT_ADMIN, scope_type__isnull=True).exists()

    @property
    def is_manager(self):
        return self.role_assignments.filter(role__code__in=(Role.Code.OPS_MANAGER, Role.Code.VENUE_ADMIN, Role.Code.SERVICE_ADMIN, Role.Code.RESOURCE_ADMIN)).exists()

    def soft_delete(self):
        self.is_active = False
        self.is_deleted = True
        self.save(update_fields=["is_active", "is_deleted"])


class UserSettings(models.Model):
    """Per-user settings stored in the same tenant schema as the account."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="settings")
    theme = models.CharField(max_length=10, default="system")
    font_size = models.CharField(max_length=10, default="medium")
    font_family = models.CharField(max_length=30, default="system")
    notification_channels = models.JSONField(default=dict, blank=True)
    notification_categories = models.JSONField(default=dict, blank=True)
    marketing_consent_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class EmailVerification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_verifications")
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class UserSession(models.Model):
    """Revocable, schema-local JWT session."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sessions")
    refresh_jti = models.CharField(max_length=255, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["refresh_jti"], condition=~models.Q(refresh_jti=""),
            name="accounts_user_session_jti_uniq",
        )]


class PasswordResetToken(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="password_reset_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


# ------------------------ SHIFT SCHEDULE (tenant) ------------------------
class ShiftSchedule(models.Model):
    """Reusable duty-timing template (e.g. Morning / Night) shared by many employees."""

    class WeekDay(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    name = models.CharField(max_length=50, unique=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_overnight = models.BooleanField(default=False)  # end_time falls on next day
    grace_minutes = models.PositiveSmallIntegerField(default=0)
    weekly_off_days = models.JSONField(default=list, blank=True, help_text="List of WeekDay values, e.g. [5, 6]")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["start_time"]

    def clean(self):
        if not self.is_overnight and self.end_time <= self.start_time:
            raise ValidationError(
                {"end_time": "End time must be after start time, or mark the shift as overnight."}
            )

    def __str__(self):
        return f"{self.name} ({self.start_time:%H:%M}–{self.end_time:%H:%M})"


# ------------------------ EMPLOYEE PROFILE (tenant) ------------------------
class EmployeeProfile(models.Model):
    """Employment record for a user inside the current tenant schema."""

    class TerminationType(models.TextChoices):
        VOLUNTARY = "VOLUNTARY", "Voluntary"
        INVOLUNTARY = "INVOLUNTARY", "Involuntary"

    class EmployeeCategory(models.TextChoices):
        REGULAR = "REGULAR", "Regular"
        FULLTIME = "FULLTIME", "Fulltime"
        PARTTIME = "PARTTIME", "Parttime"
        VIRTUAL = "VIRTUAL", "Virtual"
        PPO = "PPO", "PPO"
        VENDOR = "VENDOR", "Vendor"

    class RehiredStatus(models.TextChoices):
        YES = "YES", "Yes"
        NO = "NO", "No"

    uan_regex = RegexValidator(regex=r"^\d{12}$", message="UAN must be exactly 12 digits.")
    esi_regex = RegexValidator(regex=r"^\d{10,17}$", message="ESI number must be 10 to 17 digits.")

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="employee_profile")

    employee_id = models.CharField(max_length=20, blank=True, null=True)
    category = models.CharField(max_length=50, choices=EmployeeCategory.choices, null=True, blank=True)
    designation = models.CharField(max_length=100, blank=True, null=True, help_text="Job title, e.g. 'Senior Technician'.")
    grade = models.CharField(max_length=20, blank=True, null=True)
    cost_center = models.CharField(max_length=50, blank=True, null=True)
    department = models.CharField(
        max_length=50, blank=True, null=True,
        help_text="Employee's HR department.",
    )
    skills = models.JSONField(blank=True, null=True)

    # vendor-sourced employees only
    vendor_name = models.CharField(max_length=120, blank=True, null=True)
    vendor_phone = models.CharField(max_length=15, blank=True, null=True)

    # termination / rehire
    termination_type = models.CharField(max_length=20, choices=TerminationType.choices, blank=True, null=True)
    termination_reason = models.CharField(max_length=255, blank=True, null=True)
    last_working_day = models.DateField(blank=True, null=True, verbose_name="LWD")
    rehired_status = models.CharField(max_length=3, choices=RehiredStatus.choices, default=RehiredStatus.NO)

    # statutory: Provident Fund
    pf_applicable = models.BooleanField(default=False)
    pf_number = models.CharField(max_length=30, blank=True, null=True)
    uan_number = models.CharField(
        max_length=12, blank=True, null=True, validators=[uan_regex], help_text="12-digit Universal Account Number"
    )

    # statutory: ESI
    esi_applicable = models.BooleanField(default=False)
    esi_number = models.CharField(max_length=17, blank=True, null=True, validators=[esi_regex])
    esi_dispensary = models.CharField(max_length=120, blank=True, null=True)

    # duty timing
    shift = models.ForeignKey(ShiftSchedule, on_delete=models.SET_NULL, null=True, blank=True, related_name="employees")
    shift_effective_from = models.DateField(blank=True, null=True, help_text="Date this employee started on `shift`.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user_id"]
        indexes = [
            models.Index(fields=["category"], name="accounts_em_categor_7d3c91_idx"),
            models.Index(fields=["employee_id"], name="accounts_em_employe_15e89c_idx"),
            models.Index(fields=["pf_number"], name="accounts_em_pf_numb_9d6b64_idx"),
            models.Index(fields=["esi_number"], name="accounts_em_esi_num_6cbff9_idx"),
        ]

    def clean(self):
        if self.category != self.EmployeeCategory.VENDOR and (self.vendor_name or self.vendor_phone):
            raise ValidationError({"vendor_name": "Vendor details apply only to the VENDOR category."})
        if self.pf_applicable and not (self.pf_number or self.uan_number):
            raise ValidationError({"pf_number": "PF number or UAN is required when PF is applicable."})
        if self.esi_applicable and not self.esi_number:
            raise ValidationError({"esi_number": "ESI number is required when ESI is applicable."})
        if self.termination_type and not self.last_working_day:
            raise ValidationError({"last_working_day": "LWD is required when a termination type is set."})
        if self.last_working_day and not self.termination_type:
            raise ValidationError({"termination_type": "Termination type is required when LWD is set."})
        if self.termination_reason and not self.termination_type:
            raise ValidationError({"termination_reason": "Termination reason requires a termination type."})
        joined = self.user.date_joined
        if joined and self.last_working_day and self.last_working_day < joined.date():
            raise ValidationError({"last_working_day": "Last working day cannot precede the joining date."})

    @property
    def is_currently_employed(self):
        return not self.termination_type

    @transaction.atomic
    def terminate(self, termination_type, reason=None, last_working_day=None):
        if self.termination_type:
            raise ValidationError({"terminate": "Employee is already marked as terminated."})

        self.termination_type = termination_type
        self.termination_reason = reason
        self.last_working_day = last_working_day or timezone.now().date()

        self.full_clean()
        self.save(update_fields=["termination_type", "termination_reason", "last_working_day", "updated_at"])

        self.user.is_active = False
        self.user.is_deleted = True
        self.user.save(update_fields=["is_active", "is_deleted"])
        return self

    @transaction.atomic
    def termination_revoke(self, rehired=False):
        """Reverse a termination (rehire, or entered in error)."""
        if not self.termination_type:
            raise ValidationError({"termination_revoke": "Employee is not currently terminated."})

        self.termination_type = None
        self.termination_reason = None
        self.last_working_day = None
        self.rehired_status = self.RehiredStatus.YES if rehired else self.RehiredStatus.NO

        self.full_clean()
        self.save(update_fields=[
            "termination_type", "termination_reason", "last_working_day", "rehired_status", "updated_at",
        ])

        self.user.is_active = True
        self.user.is_deleted = False
        self.user.save(update_fields=["is_active", "is_deleted"])
        return self

    def __str__(self):
        return f"{self.employee_id or self.user_id} — {self.user.get_full_name() or self.user.email}"


# ------------------------ USER DOCUMENTS (tenant) ------------------------
class UserDocument(models.Model):
    """A document group/folder for one user (ID proofs, contracts, etc.)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="documents")
    title = models.CharField(max_length=255, blank=True, null=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="uploaded_user_documents",
    )
    remarks = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.email} - {self.title}"


class UserDocumentFile(models.Model):
    document = models.ForeignKey(UserDocument, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to="staff_documents/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.document.title} - {self.file.name}"


class StaffForHire(models.Model):
    staff_name = models.CharField(max_length=500)
    email = models.EmailField(unique=True)
    mobile_number = models.CharField(max_length=20, unique=True)
    vendor_name = models.CharField(max_length=120)
    available_from = models.DateField()
    available_for = models.JSONField(blank=True, null=True)
    language = models.JSONField(blank=True, null=True)
    skill = models.JSONField(blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    is_available = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_hire_listings")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["staff_name", "vendor_name"], name="uniq_staff_vendor_listing")]

    def __str__(self):
        return f"{self.staff_name} | {self.vendor_name}"


# ------------------------ ROLES (use Django's auth.Permission) ------------------------
class Role(models.Model):
    class Code(models.TextChoices):
        SUPER_ADMIN = "SUPER_ADMIN", "Super Admin"
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
    permissions = models.ManyToManyField("auth.Permission", blank=True, related_name="roles")

    def __str__(self):
        return self.name


class RoleAssignment(models.Model):
    """Grants a role tenant-wide or on one object (Tenant, Venue, Service, Resource)."""

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
