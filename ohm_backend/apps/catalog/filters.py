import django_filters
from .models import Resource, Service, Venue


class VenueFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    city = django_filters.CharFilter(field_name="location__city", lookup_expr="icontains")
    state = django_filters.CharFilter(field_name="location__state", lookup_expr="icontains")
    locality = django_filters.CharFilter(field_name="location__locality", lookup_expr="icontains")
    min_capacity = django_filters.NumberFilter(field_name="capacity", lookup_expr="gte")
    max_capacity = django_filters.NumberFilter(field_name="capacity", lookup_expr="lte")
    min_price = django_filters.NumberFilter(field_name="price_per_event", lookup_expr="gte")
    max_price = django_filters.NumberFilter(field_name="price_per_event", lookup_expr="lte")
    is_active = django_filters.BooleanFilter()
    external_decorators_allow = django_filters.BooleanFilter()
    external_caterers_allow = django_filters.BooleanFilter()

    class Meta:
        model = Venue
        fields = [
            "name",
            "city",
            "state",
            "locality",
            "is_active",
            "min_capacity",
            "max_capacity",
            "min_price",
            "max_price",
            "external_decorators_allow",
            "external_caterers_allow",
        ]


class ServiceFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    city = django_filters.CharFilter(lookup_expr="icontains")
    service_type = django_filters.CharFilter(lookup_expr="exact")
    venue = django_filters.ModelChoiceFilter(queryset=Venue.objects.filter(is_deleted=False))
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = Service
        fields = ["name", "city", "service_type", "venue", "is_active"]


class ResourceFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    venue = django_filters.ModelChoiceFilter(queryset=Venue.objects.filter(is_deleted=False))
    service = django_filters.ModelChoiceFilter(queryset=Service.objects.filter(is_deleted=False))
    min_available = django_filters.NumberFilter(field_name="available_quantity", lookup_expr="gte")
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = Resource
        fields = ["name", "venue", "service", "min_available", "is_active"]
