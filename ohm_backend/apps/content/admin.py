from django.contrib import admin

from .models import Contact, FAQItem, FAQTopic, Video


class FAQItemInline(admin.TabularInline):
    model = FAQItem
    extra = 1
    fields = ("question", "answer", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")


@admin.register(FAQTopic)
class FAQTopicAdmin(admin.ModelAdmin):
    list_display = ("id", "topic", "created_at", "updated_at")
    search_fields = ("topic",)
    list_filter = ("created_at",)
    ordering = ("-created_at",)
    inlines = [FAQItemInline]


@admin.register(FAQItem)
class FAQItemAdmin(admin.ModelAdmin):
    list_display = ("id", "short_question", "topic", "created_at")
    search_fields = ("question", "answer", "topic__topic")
    list_filter = ("topic", "created_at")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")

    def short_question(self, obj):
        return obj.question[:50]

    short_question.short_description = "Question"


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("display_name", "platform", "mobile_number", "email", "url", "created_at")
    list_filter = ("platform", "created_at")
    search_fields = ("display_name", "email", "mobile_number")


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ("video_name", "video_url", "created_at")
    search_fields = ("video_name",)
