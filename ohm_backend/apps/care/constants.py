from django.db import models


class BookingType(models.TextChoices):
    IN_HOUSE = "IN_HOUSE", "In House"
    OPD = "OPD", "OPD"
    CLIENT_SIDE = "CLIENT_SIDE", "Client Side"


class PeriodChoices(models.TextChoices):
    HOURLY = "HOURLY", "Hourly"
    DAILY = "DAILY", "Daily"
    WEEKLY = "WEEKLY", "Weekly"
    MONTHLY = "MONTHLY", "Monthly"
    ONCE = "ONCE", "Once"


class BookingStatus(models.TextChoices):
    LOBBY = "LOBBY", "Lobby"
    HOLD = "HOLD", "Hold"
    BOOKED = "BOOKED", "Booked"
    DELAYED = "DELAYED", "Delayed"
    CANCELLED = "CANCELLED", "Cancelled"
    FULFILLED = "FULFILLED", "Fulfilled"
    UNFULFILLED = "UNFULFILLED", "Unfulfilled"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    YET_TO_START = "YET_TO_START", "Yet To Start"
    PARTIALLY_FULFILLED = "PARTIALLY_FULFILLED", "Partially Fulfilled"
    COMPLETED = "COMPLETED", "Completed"


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    UPI = "UPI", "UPI"
    CARD = "CARD", "Card"
    BANK = "BANK", "Bank Transfer"
    CHEQUE = "CHEQUE", "Cheque"
    RAZORPAY = "RAZORPAY", "Razorpay"


class AttendanceStatusCode(models.TextChoices):
    PRESENT = "PRESENT", "Present"
    ABSENT = "ABSENT", "Absent"
    HALF_DAY = "HALF_DAY", "Half Day"
    LEAVE = "LEAVE", "Leave"
    HOLIDAY = "HOLIDAY", "Holiday"
