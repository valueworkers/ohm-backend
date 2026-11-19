from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .serializers import NotificationSerializer


def push_notification(notification):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    group_name = "Public"
    serializer = NotificationSerializer(notification)

    try:
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "send_notification",
                "data": serializer.data,
            },
        )
    except Exception:
        pass
