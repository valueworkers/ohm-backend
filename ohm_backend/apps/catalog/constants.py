from django.db import models


class EntityType(models.TextChoices):
    VENUE = "venue", "Venue"
    SERVICE = "service", "Service"
    RESOURCE = "resource", "Resource"


ALLOWED_PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_PHOTO_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
