from django.contrib.auth import get_user_model
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification, NotificationTemplate
from .serializers import (
    MarkReadSerializer,
    NotificationCreateSerializer,
    NotificationSerializer,
    NotificationSubscriberSerializer,
    NotificationTemplateSerializer,
)
from .utils import push_notification

User = get_user_model()


def is_admin_or_manager(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user.groups.filter(name__in=["Tenant Admin", "Super User"]).exists()


class NotificationTemplateViewSet(viewsets.ModelViewSet):
    """CRUD for notification templates."""
    serializer_class = NotificationTemplateSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["channel", "category", "is_active"]
    search_fields = ["name", "subject", "body"]
    ordering_fields = ["name", "created_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        user = self.request.user
        if is_admin_or_manager(user):
            return NotificationTemplate.objects.all()
        return NotificationTemplate.objects.filter(owner=user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class NotificationViewSet(viewsets.ModelViewSet):
    """
    Full CRUD for notifications.
    """
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["channel", "category", "priority", "status"]
    search_fields = ["title", "message"]
    ordering_fields = ["created_at", "priority", "status"]
    ordering = ["-created_at"]
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Notification.objects.select_related("owner", "recipient", "template")
        if is_admin_or_manager(user):
            return qs
        return qs.filter(recipient=user)

    def get_serializer_class(self):
        if self.action == "create":
            return NotificationCreateSerializer
        return NotificationSerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=["post"], url_path="mark_read")
    def mark_read(self, request, pk=None):
        notification = self.get_object()
        notification.mark_read()
        push_notification(notification)
        return Response({"detail": "Notification marked as read."}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="mark_read_bulk")
    def mark_read_bulk(self, request):
        serializer = MarkReadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        updated = (
            Notification.objects.filter(id__in=ids, recipient=request.user)
            .exclude(status=Notification.Status.READ)
        )
        count = updated.update(status=Notification.Status.READ, read_at=timezone.now())
        return Response({"detail": f"{count} notification(s) marked as read."}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="unread_count")
    def unread_count(self, request):
        count = Notification.objects.filter(recipient=request.user).exclude(status=Notification.Status.READ).count()
        return Response({"unread_count": count}, status=status.HTTP_200_OK)


class NotificationSubscriberView(APIView):
    """
    Receives events and creates Notification records.
    """
    def post(self, request):
        serializer = NotificationSubscriberSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            recipient = User.objects.get(id=data["recipient_id"])
        except User.DoesNotExist:
            return Response({"error": "Recipient not found"}, status=404)

        template = None
        message = data.get("message", "")
        title = data.get("title", "")

        if data.get("template_name"):
            template = NotificationTemplate.objects.filter(
                name=data["template_name"],
                channel=data["channel"],
                is_active=True,
            ).first()

            if template:
                message = self.render_template(template.body, data.get("extra_data", {}))
                title = template.subject or title

        notification = Notification.objects.create(
            recipient=recipient,
            template=template,
            channel=data["channel"],
            category=data["category"],
            title=title,
            message=message,
            extra_data=data.get("extra_data", {}),
            recipient_email=getattr(recipient, "email", "") or "",
            recipient_phone=getattr(recipient, "phone_number", "") or "",
            status=Notification.Status.PENDING,
        )

        return Response({"message": "Notification created", "id": notification.id}, status=status.HTTP_201_CREATED)

    def render_template(self, body, context):
        for key, value in context.items():
            body = body.replace(f"{{{{ {key} }}}}", str(value))
        return body
