from django.contrib.auth.password_validation import validate_password as django_validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from .models import User
from .services import set_role

ROLE_CHOICES = [("TENANT_ADMIN", "Tenant Admin"), ("CUSTOMER", "Customer / User")]


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
    full_name = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")

    def validate_email(self, value):
        return _check_email(value)

    def validate_password(self, value):
        return _check_password(value)


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
    purpose = serializers.ChoiceField(choices=("login", "password_reset"))


class OTPPasswordResetSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=254)
    otp = serializers.CharField(min_length=6, max_length=6)
    new_password = serializers.CharField(trim_whitespace=False)

    def validate_new_password(self, value):
        return _check_password(value)


class UserSerializer(serializers.ModelSerializer):
    """Tenant Admin's view of the workspace's users (Tenant Admins and Customers)."""

    role = serializers.ChoiceField(choices=ROLE_CHOICES, source="primary_role", required=False)
    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "phone", "is_active", "role", "password", "date_joined"]
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
        set_role(user, role, assigned_by=self.context["request"].user)
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
            set_role(instance, role, assigned_by=self.context["request"].user)
        return instance
