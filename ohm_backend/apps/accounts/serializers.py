import re

from django.contrib.auth.password_validation import validate_password as django_validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from django.contrib.auth.models import Permission

from .models import (
    EmployeeProfile, Role, RoleAssignment, ShiftSchedule, StaffForHire,
    User, UserDocument, UserDocumentFile
)
from .services import AccountsService




def _check_password(value):
    try:
        django_validate_password(value)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(list(exc.messages))
    return value


def _check_email(value, instance=None):
    value = value.strip().lower()
    qs = User.objects.filter(email__iexact=value)
    if instance is not None:
        qs = qs.exclude(pk=instance.pk)
    if qs.exists():
        raise serializers.ValidationError("This email is already registered.")
    return value


class CustomerRegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    first_name = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    middle_name = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    last_name = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    mobile_number = serializers.CharField(max_length=10, required=False, allow_blank=True, allow_null=True, default=None)

    def validate_email(self, value):
        return _check_email(value)

    def validate_password(self, value):
        return _check_password(value)


class EmployeeProfileSerializer(serializers.ModelSerializer):
    shift_detail = serializers.StringRelatedField(source="shift", read_only=True)

    class Meta:
        model = EmployeeProfile
        exclude = ("user",)
        extra_kwargs = {"shift": {"write_only": True, "required": False}}

    def validate(self, attrs):
        get = lambda name, default=None: attrs.get(name, getattr(self.instance, name, default))
        category = get("category")
        if category != EmployeeProfile.EmployeeCategory.VENDOR and (get("vendor_name") or get("vendor_phone")):
            raise serializers.ValidationError({"vendor_name": "Vendor details apply only to the VENDOR category."})
        if get("pf_applicable", False) and not (get("pf_number") or get("uan_number")):
            raise serializers.ValidationError({"pf_number": "PF number or UAN is required when PF is applicable."})
        if get("esi_applicable", False) and not get("esi_number"):
            raise serializers.ValidationError({"esi_number": "ESI number is required when ESI is applicable."})
        return attrs


class ShiftScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShiftSchedule
        fields = "__all__"

    def validate_weekly_off_days(self, days):
        if not isinstance(days, list) or any(not isinstance(day, int) or day < 0 or day > 6 for day in days):
            raise serializers.ValidationError("Use a list of weekday values from 0 to 6.")
        if len(set(days)) != len(days):
            raise serializers.ValidationError("Duplicate weekday values are not allowed.")
        return days

    def validate(self, attrs):
        start = attrs.get("start_time", getattr(self.instance, "start_time", None))
        end = attrs.get("end_time", getattr(self.instance, "end_time", None))
        overnight = attrs.get("is_overnight", getattr(self.instance, "is_overnight", False))
        if start is not None and end is not None and not overnight and end <= start:
            raise serializers.ValidationError({"end_time": "End time must be after start time, or mark the shift as overnight."})
        return attrs


class MedicareUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)
    confirm_password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)
    employee_profile = EmployeeProfileSerializer(read_only=True)

    class Meta:
        model = User
        fields = (
            "id", "email", "profile_pic", "first_name", "middle_name", "last_name","primary_role",
            "mobile_number", "alternate_phone_number", "emergency_contact_name", "emergency_contact_number",
            "age", "gender", "permanent_address", "current_address", "city", "is_active", "is_deleted",
            "created_by", "date_joined", "password", "confirm_password", "employee_profile",
        )
        read_only_fields = ("id", "created_by", "date_joined", "is_deleted")

    def validate(self, attrs):
        password = attrs.pop("password", None)
        confirm = attrs.pop("confirm_password", None)
        if self.instance is None and not password:
            raise serializers.ValidationError({"password": "This field is required."})
        if password:
            if password != confirm:
                raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
            password = _check_password(password)
            attrs["_password"] = password
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("_password", None)
        request = self.context.get("request")
        if request and request.user.is_authenticated and (request.user.is_owner or request.user.is_manager):
            validated_data["created_by"] = request.user
        user = User(**validated_data)
        user.set_password(password) if password else user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("_password", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class MedicareEmployeeSerializer(MedicareUserSerializer):
    employee_profile = EmployeeProfileSerializer(required=False)
    role = serializers.ChoiceField(choices=[(code, code.replace("_", " ").title()) for code in (Role.Code.STAFF, Role.Code.OPS_MANAGER, Role.Code.VENUE_ADMIN, Role.Code.SERVICE_ADMIN, Role.Code.RESOURCE_ADMIN)], required=False, write_only=True)

    class Meta(MedicareUserSerializer.Meta):
        fields = MedicareUserSerializer.Meta.fields + ("role",)

    def create(self, validated_data):
        profile_data = validated_data.pop("employee_profile", {})
        role_code = validated_data.pop("role", Role.Code.STAFF)
        user = super().create(validated_data)
        EmployeeProfile.objects.create(user=user, **profile_data)
        role = Role.objects.filter(code=role_code).first()
        if role:
            RoleAssignment.objects.get_or_create(user=user, role=role)
        return user

    def update(self, instance, validated_data):
        profile_data = validated_data.pop("employee_profile", None)
        role_code = validated_data.pop("role", None)
        user = super().update(instance, validated_data)
        if profile_data is not None:
            profile, _ = EmployeeProfile.objects.get_or_create(user=user)
            for key, value in profile_data.items():
                setattr(profile, key, value)
            profile.full_clean()
            profile.save()
        if role_code:
            AccountsService.set_role(user, role_code, assigned_by=self.context.get("request").user)
        return user


class CustomerListSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "profile_pic", "first_name", "middle_name", "last_name", "email", "mobile_number", "city", "is_active")


class UserDocumentFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserDocumentFile
        fields = "__all__"


class UserDocumentSerializer(serializers.ModelSerializer):
    files = UserDocumentFileSerializer(many=True, read_only=True)

    class Meta:
        model = UserDocument
        fields = "__all__"
        read_only_fields = ("uploaded_by",)


class StaffForHireSerializer(serializers.ModelSerializer):
    class Meta:
        model = StaffForHire
        fields = "__all__"
        read_only_fields = ("created_by", "created_at", "updated_at")

    def create(self, validated_data):
        request = self.context.get("request")
        if request:
            validated_data["created_by"] = request.user
        return super().create(validated_data)


class BulkEmployeeUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, value):
        if not value.name.lower().endswith((".xlsx", ".xlsm")):
            raise serializers.ValidationError("Upload an .xlsx or .xlsm workbook.")
        if value.size > 10 * 1024 * 1024:
            raise serializers.ValidationError("File must be no larger than 10 MB.")
        return value


class EmployeeTerminationSerializer(serializers.Serializer):
    termination_type = serializers.ChoiceField(choices=EmployeeProfile.TerminationType.choices)
    reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    last_working_day = serializers.DateField(required=False)


class UnifiedLoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=254)
    method = serializers.ChoiceField(choices=("password", "otp"))
    password = serializers.CharField(required=False, allow_blank=False, trim_whitespace=False)
    otp = serializers.CharField(required=False, min_length=6, max_length=6)

    def validate(self, attrs):
        credential = "password" if attrs["method"] == "password" else "otp"
        if not attrs.get(credential):
            raise serializers.ValidationError({credential: "This field is required for the selected login method."})
        return attrs


class OTPRequestSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=254)
    purpose = serializers.ChoiceField(choices=("login", "password_reset"), required=False, default="login")


class OTPPasswordResetSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=254)
    otp = serializers.CharField(min_length=6, max_length=6)
    new_password = serializers.CharField(trim_whitespace=False)

    def validate_new_password(self, value):
        return _check_password(value)


class OwnerSerializer(serializers.ModelSerializer):
    """Read + self/owner update. Email is the OTP login identity, so it is read-only."""

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "middle_name", "last_name", "mobile_number", "is_active", "date_joined"]
        read_only_fields = ["id", "email", "is_active", "date_joined"]


class OwnerCreateSerializer(serializers.ModelSerializer):
    """Add a co-owner. No password: owners sign in with OTP only."""

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "middle_name", "last_name", "mobile_number"]
        read_only_fields = ["id"]

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value
    
    @transaction.atomic
    def create(self, validated_data):
        user = User(**validated_data, is_active=True)
        user.set_unusable_password()
        user.save()
        AccountsService.assign_tenant_admin_permissions(user)

        from django.db import connection
        from django_tenants.utils import get_public_schema_name, schema_context
        from apps.accounts.signals import _get_or_create_public_user
        from apps.platform.models import Tenant

        current_schema = connection.schema_name
        if current_schema != get_public_schema_name():
            public_user = _get_or_create_public_user(user.email)
            updated_fields = []
            for field in ("first_name", "middle_name", "last_name", "mobile_number"):
                value = getattr(user, field)
                if value and getattr(public_user, field) != value:
                    setattr(public_user, field, value)
                    updated_fields.append(field)
            if not public_user.is_active:
                public_user.is_active = True
                updated_fields.append("is_active")
            if updated_fields:
                public_user.save(update_fields=updated_fields)

            with schema_context(get_public_schema_name()):
                tenant = Tenant.objects.filter(schema_name=current_schema).first()
                if tenant:
                    tenant.owners.add(public_user)

        # transaction.on_commit(lambda: send_owner_welcome_email.delay(user.pk))
        return user

class UserSerializer(serializers.ModelSerializer):
    """Tenant Admin's view of the workspace's users (Tenant Admins and Customers)."""

    role = serializers.ChoiceField(choices=Role.Code.choices, source="primary_role", required=False)
    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)
    employee_profile = EmployeeProfileSerializer(read_only=True)

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "middle_name", "last_name", "mobile_number", "is_active", "role", "password", "date_joined", "employee_profile"]
        read_only_fields = ["id", "date_joined"]
        extra_kwargs = {"email": {"validators": []}}

    def validate_email(self, value):
        return _check_email(value, self.instance)

    def validate_password(self, value):
        return _check_password(value)

    def validate(self, attrs):
        if self.instance is None:
            if not attrs.get("password"):
                raise serializers.ValidationError({"password": "This field is required."})
        elif "email" in attrs and attrs["email"] != self.instance.email:
            raise serializers.ValidationError({"email": "Email cannot be changed."})
        return attrs

    def _guard_last_admin(self, user, is_active, role):
        """Never leave a workspace without an active Tenant Admin, and never self-deactivate."""
        if not (user.is_active and user.primary_role == "TENANT_ADMIN"):
            return
        losing = is_active is False or (role is not None and role != "TENANT_ADMIN")
        if not losing:
            return
        if is_active is False and user.pk == self.context["request"].user.pk:
            raise serializers.ValidationError("You cannot deactivate your own account.")
        others = (
            User.objects.filter(
                is_active=True, role_assignments__role__code="TENANT_ADMIN", role_assignments__scope_type__isnull=True
            )
            .exclude(pk=user.pk)
            .exists()
        )
        if not others:
            raise serializers.ValidationError("A workspace must keep at least one active Tenant Admin.")

    @transaction.atomic
    def create(self, validated):
        role = validated.pop("primary_role", "CUSTOMER")
        password = validated.pop("password")
        user = User.objects.create_user(password=password, **validated)
        AccountsService.set_role(user, role, assigned_by=self.context["request"].user)
        return user

    @transaction.atomic
    def update(self, instance, validated):
        role = validated.pop("primary_role", None)
        validated.pop("password", None)
        validated.pop("email", None)
        self._guard_last_admin(instance, validated.get("is_active"), role)
        for key, value in validated.items():
            setattr(instance, key, value)
        instance.save()
        if role:
            AccountsService.set_role(instance, role, assigned_by=self.context["request"].user)
        return instance


class RoleSerializer(serializers.ModelSerializer):
    code = serializers.CharField(max_length=30)
    permissions = serializers.PrimaryKeyRelatedField(
        queryset=Permission.objects.all(), many=True, required=False, write_only=True
    )
    permission_details = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = ("id", "code", "name", "rank", "permissions", "permission_details")
        read_only_fields = ("id", "permission_details")

    def get_permission_details(self, role):
        return [
            {"id": permission.id, "code": permission.codename, "name": permission.name}
            for permission in role.permissions.order_by("codename")
        ]

    def validate_code(self, value):
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,29}", value):
            raise serializers.ValidationError("Use 1–30 uppercase letters, numbers, or underscores; start with a letter.")
        if value in {Role.Code.TENANT_ADMIN, Role.Code.CUSTOMER} and not (
            self.instance and self.instance.code == value
        ):
            raise serializers.ValidationError("This code is reserved for a built-in role.")
        existing = Role.objects.filter(code=value)
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A role with this code already exists.")
        return value

    def validate_rank(self, value):
        if value < 1 or value >= 100:
            raise serializers.ValidationError("Custom role rank must be between 1 and 99.")
        return value

    def validate(self, attrs):
        if self.instance and self.instance.code in {Role.Code.TENANT_ADMIN, Role.Code.CUSTOMER}:
            if attrs.get("code", self.instance.code) != self.instance.code:
                raise serializers.ValidationError({"code": "Built-in role codes cannot be changed."})
            if attrs.get("rank", self.instance.rank) != self.instance.rank:
                raise serializers.ValidationError({"rank": "Built-in role ranks cannot be changed."})
        return attrs


class RoleAssignmentSerializer(serializers.ModelSerializer):
    user_id = serializers.PrimaryKeyRelatedField(source="user", queryset=User.objects.all(), write_only=True)
    role_id = serializers.PrimaryKeyRelatedField(source="role", queryset=Role.objects.all(), write_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    role_code = serializers.CharField(source="role.code", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)
    object_type = serializers.CharField(write_only=True, required=False, allow_blank=True)
    object_id = serializers.IntegerField(write_only=True, required=False, min_value=1)
    scope = serializers.SerializerMethodField()

    class Meta:
        model = RoleAssignment
        fields = (
            "id", "user_id", "user_email", "role_id", "role_code", "role_name",
            "object_type", "object_id", "scope", "created_at",
        )
        read_only_fields = ("id", "created_at", "scope")

    def get_scope(self, assignment):
        if assignment.scope_type_id is None:
            return None
        return {"object_type": f"{assignment.scope_type.app_label}.{assignment.scope_type.model}",
                "object_id": assignment.scope_id}

    def validate(self, attrs):
        object_type = attrs.pop("object_type", "")
        object_id = attrs.pop("object_id", None)
        if bool(object_type) != bool(object_id):
            raise serializers.ValidationError({"scope": "Provide both object_type and object_id, or neither."})
        attrs["scope_type"] = None
        attrs["scope_id"] = None
        if object_type:
            try:
                app_label, model_name = object_type.lower().split(".", 1)
                content_type = ContentType.objects.get(app_label=app_label, model=model_name)
                model = content_type.model_class()
                tenant_app_labels = {app.split(".")[-1] for app in settings.TENANT_APPS}
                if (model is None or model._meta.proxy or model._meta.app_label not in tenant_app_labels
                        or model._meta.app_label == "accounts"):
                    raise ContentType.DoesNotExist
                model.objects.get(pk=object_id)
            except (ValueError, ContentType.DoesNotExist):
                raise serializers.ValidationError({"object_type": "Choose an existing object type and object in this tenant."})
            except model.DoesNotExist:
                raise serializers.ValidationError({"object_id": "The object was not found in this tenant."})
            attrs["scope_type"] = content_type
            attrs["scope_id"] = object_id
        return attrs

    def create(self, validated_data):
        validated_data["assigned_by"] = self.context["request"].user
        assigned_by = validated_data.pop("assigned_by")
        assignment, _ = RoleAssignment.objects.get_or_create(
            user=validated_data["user"], role=validated_data["role"],
            scope_type=validated_data["scope_type"], scope_id=validated_data["scope_id"],
            defaults={"assigned_by": assigned_by},
        )
        return assignment
