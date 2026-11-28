import django_filters

from .models import Patient

class PatientFilter(django_filters.FilterSet):
    onboading_date = django_filters.CharFilter(field_name='onboading_date',label='Onboading Date')
    location_type = django_filters.CharFilter(field_name='location_type',label='Location Type')
    booking_locality = django_filters.CharFilter(field_name='booking_locality',label='Booking Locality')

    class Meta:
        model = Patient
        fields = [
            "location_type",
            "booking_locality",
            "gender",
            "blood_group",
            "registered_by",
            "onboading_date",
            "is_probono",
            "is_registration_fees_paid",
            "is_deleted",
            "is_active",
        ]