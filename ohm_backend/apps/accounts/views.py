from django.db import connection, transaction
from django.db.models import Q
from rest_framework import mixins, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.accounts.auth import TenantTokenObtainPairSerializer
from apps.accounts.models import Role, RoleAssignment, User
from apps.accounts.permissions import IsTenantAdmin
from apps.accounts.serializers import CustomerRegisterSerializer, UserSerializer


class TenantLoginView(TokenObtainPairView):
    """Log in a tenant user and return tenant-bound JWTs."""

    serializer_class = TenantTokenObtainPairSerializer


class TenantCurrentSessionView(APIView):
    """Return the current authenticated tenant user's profile and workspace."""

    def get(self, request):
        user = request.user
        tenant = getattr(connection, "tenant", None)
        return Response({
            "email": user.email,
            "full_name": user.full_name,
            "workspace": getattr(tenant, "name", None),
            "modules": getattr(tenant, "modules", []),
            "roles": [
                {
                    "code": assignment.role.code,
                    "scope_type": assignment.scope_type.model if assignment.scope_type else None,
                    "scope_id": assignment.scope_id,
                }
                for assignment in user.role_assignments.select_related("role", "scope_type")
            ],
        })


class TenantRegistrationCreateView(APIView):
    """Let a provider's customers create login accounts in that provider's schema."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "tenant_registration_create"

    def post(self, request):
        serializer = CustomerRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            user = User.objects.create_user(
                email=data["email"],
                password=data["password"],
                full_name=data["full_name"],
                phone=data["phone"],
            )
            RoleAssignment.objects.create(user=user, role=Role.objects.get(code=Role.Code.CUSTOMER))
        return Response({"email": user.email}, status=201)


class TenantUserViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Let a provider's Tenant Admin manage accounts stored in that provider's schema."""

    serializer_class = UserSerializer
    permission_classes = [IsTenantAdmin]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        queryset = User.objects.all().order_by("id").prefetch_related("role_assignments__role")
        role = self.request.query_params.get("role")
        if role:
            queryset = queryset.filter(role_assignments__role__code=role, role_assignments__scope_type__isnull=True)
        search = self.request.query_params.get("q")
        if search:
            queryset = queryset.filter(Q(email__icontains=search) | Q(full_name__icontains=search))
        return queryset.distinct()
