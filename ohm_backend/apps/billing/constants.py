"""
apps/billing/constants.py
─────────────────────────
Shared choices and transition maps for the billing / booking domain.

Includes booking entities, quote statuses, invoice statuses,
line item types, units of measure, and payment methods.
"""

from django.db import models


# ---------------------------------------------------------------------------
# Booking-domain choices
# ---------------------------------------------------------------------------

class BookingType(models.TextChoices):
    IN_HOUSE = "IN_HOUSE", "In House"
    OPD = "OPD", "OPD"
    CLIENT_SIDE = "CLIENT_SIDE", "Client Side"


class PeriodChoices(models.TextChoices):
    HOURLY = "HOURLY", "Hourly"
    DAILY = "DAILY", "Daily"
    WEEKLY = "WEEKLY", "Weekly"
    MONTHLY = "MONTHLY", "Monthly"


class BookingEntity(models.TextChoices):
    VENUE = "VENUE", "Venue"
    SERVICE = "SERVICE", "Service"
    RESOURCE = "RESOURCE", "Resource"


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
    MODIFIED = "MODIFIED", "Modified"
    RESCHEDULED = "RESCHEDULED", "Rescheduled"


# Allowed manual status transitions
MANUAL_STATUS_TRANSITIONS: dict[str, list[str]] = {
    BookingStatus.LOBBY: [
        BookingStatus.BOOKED,
        BookingStatus.CANCELLED,
    ],
    BookingStatus.BOOKED: [
        BookingStatus.CANCELLED,
        BookingStatus.HOLD,
        BookingStatus.DELAYED,
    ],
    BookingStatus.YET_TO_START: [
        BookingStatus.CANCELLED,
        BookingStatus.HOLD,
        BookingStatus.MODIFIED,
        BookingStatus.RESCHEDULED,
    ],
    BookingStatus.IN_PROGRESS: [
        BookingStatus.CANCELLED,
        BookingStatus.UNFULFILLED,
        BookingStatus.PARTIALLY_FULFILLED,
        BookingStatus.MODIFIED,
        BookingStatus.RESCHEDULED,
    ],
    BookingStatus.HOLD: [
        BookingStatus.BOOKED,
        BookingStatus.IN_PROGRESS,
        BookingStatus.CANCELLED,
    ],
    BookingStatus.UNFULFILLED: [
        BookingStatus.FULFILLED,
        BookingStatus.PARTIALLY_FULFILLED,
    ],
    BookingStatus.PARTIALLY_FULFILLED: [
        BookingStatus.UNFULFILLED,
        BookingStatus.IN_PROGRESS,
    ],
}


# ---------------------------------------------------------------------------
# Quotation-domain choices
# ---------------------------------------------------------------------------

class QuoteStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SENT = "sent", "Sent"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"
    SUPERSEDED = "superseded", "Superseded"


class QuoteSupplyType(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    INTRA_STATE = "intra_state", "Intra-state (CGST + SGST)"
    INTRA_UT = "intra_ut", "Intra-UT (CGST + UTGST)"
    INTER_STATE = "inter_state", "Inter-state (IGST)"


class QuoteLineKind(models.TextChoices):
    CATALOG = "catalog", "Catalog"
    CUSTOM = "custom", "Custom service"
    VENUE = "venue", "Venue"
    SERVICE = "service", "Service"
    RESOURCE = "resource", "Resource"
    SERVICE_VISIT = "service_visit", "Service visit"
    MANPOWER = "manpower", "Man power"
    ON_REQUEST = "on_request", "On request"


class Uom(models.TextChoices):
    # Quantity / Count
    PCS = "PCS", "Pcs"
    NOS = "NOS", "Nos"
    UNT = "UNT", "Unit"
    SET = "SET", "Set"
    PR = "PR", "Pair"
    DOZ = "DOZ", "Dozen"

    # Packaging
    BOX = "BOX", "Box"
    PAC = "PAC", "Pack"
    PKT = "PKT", "Packet"
    CTN = "CTN", "Carton"
    BAG = "BAG", "Bag"
    BDL = "BDL", "Bundle"
    ROL = "ROL", "Roll"
    BTL = "BTL", "Bottle"
    CAN = "CAN", "Can"

    # Weight
    MG = "MG", "Mg"
    GMS = "GMS", "Gms"
    KGS = "KGS", "Kg"
    QTL = "QTL", "Quintal"
    TON = "TON", "Ton"

    # Volume
    ML = "ML", "Ml"
    LTR = "LTR", "Litre"

    # Length / Distance
    MM = "MM", "Mm"
    CM = "CM", "Cm"
    MTR = "MTR", "Mtr"
    KM = "KM", "Km"
    IN = "IN", "Inch"
    FT = "FT", "Feet"
    YD = "YD", "Yard"
    MI = "MI", "Mile"

    # Area
    SQFT = "SQFT", "Sq.Ft"
    SQM = "SQM", "Sq.Mtr"
    SQYD = "SQYD", "Sq.Yard"

    # Time / Services
    HRS = "HRS", "Hour"
    DAY = "DAY", "Day"
    MTH = "MTH", "Month"
    YR = "YR", "Year"
    VST = "VST", "Visits"

    # Other
    OTH = "OTH", "Other"


# ---------------------------------------------------------------------------
# Invoice-domain choices
# ---------------------------------------------------------------------------

class InvoiceStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ISSUED = "ISSUED", "Issued"
    UNPAID = "UNPAID", "Unpaid"
    PARTIALLY_PAID = "PARTIALLY_PAID", "Partially Paid"
    PAID = "PAID", "Paid"
    OVERDUE = "OVERDUE", "Overdue"
    VOID = "VOID", "Void"
    CANCELLED = "CANCELLED", "Cancelled"
    REFUNDED = "REFUNDED", "Refunded"


class InvoiceType(models.TextChoices):
    STANDARD = "STANDARD", "Standard"
    QUOTATION = "QUOTATION", "Quotation Invoice"
    VENUE = "VENUE", "Venue Invoice"
    SERVICE = "SERVICE", "Service Invoice"
    RESOURCE = "RESOURCE", "Resource Invoice"
    BOOKING_SECONDARY = "BOOKING_SECONDARY", "Booking Secondary Order"
    BOOKING_TERNARY = "BOOKING_TERNARY", "Booking Ternary Order"


class InvoiceLineType(models.TextChoices):
    CATALOG = "catalog", "Catalog"
    CUSTOM = "custom_service", "Custom service"
    VENUE = "venue", "Venue"
    SERVICE = "service", "Service"
    RESOURCE = "resource", "Resource"
    VISIT = "service_visit", "Service visit"
    MANPOWER = "man_power", "Man power"


# ---------------------------------------------------------------------------
# Payment-domain choices
# ---------------------------------------------------------------------------

class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    UPI = "UPI", "UPI"
    CARD = "CARD", "Card"
    BANK = "BANK", "Bank"
    CHEQUE = "CHEQUE", "Cheque"
    RAZORPAY = "RAZORPAY", "Razorpay"
    WALLET = "WALLET", "Wallet"
    WALLET_REFUND = "WALLET_REFUND", "Wallet Refund"


PAYMENT_METHOD_NORMALIZE = {
    "cash": PaymentMethod.CASH,
    "upi": PaymentMethod.UPI,
    "card": PaymentMethod.CARD,
    "bank": PaymentMethod.BANK,
    "bank_transfer": PaymentMethod.BANK,
    "netbanking": PaymentMethod.BANK,
    "cheque": PaymentMethod.CHEQUE,
    "razorpay": PaymentMethod.RAZORPAY,
    "wallet": PaymentMethod.WALLET,
    "wallet_refund": PaymentMethod.WALLET_REFUND,
}


class PaymentMappingStatus(models.TextChoices):
    AUTO_MAPPED = "AUTO_MAPPED", "Auto Mapped"
    UNMAPPED = "UNMAPPED", "Unmapped"
    REVIEW = "REVIEW", "Review Required"
    MANUAL = "MANUAL", "Manually Mapped"
