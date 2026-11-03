"""Indian GST helpers: GSTIN validation, state codes, supply type, amount in words.

Pure Python, no Django imports, so it is safe to use from models, services,
serializers and tests.
"""
import re
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

GST_RATES = [Decimal(r) for r in (
    "0", "0.1", "0.25", "1", "1.5", "3", "5", "6", "7.5", "12", "18", "28", "40",
)]

STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram",
    "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
    "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh",
    "38": "Ladakh", "96": "Other Countries", "97": "Other Territory",
}

# Union territories without a legislature charge UTGST instead of SGST.
UT_WITHOUT_LEGISLATURE = {"04", "26", "31", "35", "38"}

_STATE_ALIASES = {
    "orissa": "21", "pondicherry": "34", "nct of delhi": "07", "new delhi": "07",
    "jammu & kashmir": "01", "andaman & nicobar islands": "35",
    "dadra and nagar haveli": "26", "daman and diu": "26",
    "dadra & nagar haveli and daman & diu": "26", "uttaranchal": "05",
}
STATE_BY_NAME = {name.casefold(): code for code, name in STATE_CODES.items()}
STATE_BY_NAME.update(_STATE_ALIASES)

INTRA_STATE, INTRA_UT, INTER_STATE, UNKNOWN = "intra_state", "intra_ut", "inter_state", "unknown"


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def gstin_checksum(first14):
    total = 0
    for i, ch in enumerate(first14):
        v = _CHARSET.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return _CHARSET[(36 - total % 36) % 36]


def is_valid_gstin(gstin):
    gstin = (gstin or "").strip().upper()
    return bool(
        GSTIN_RE.match(gstin)
        and gstin[:2] in STATE_CODES
        and gstin[14] == gstin_checksum(gstin[:14])
    )


def state_code_from_gstin(gstin):
    """First two digits of a GSTIN if they are a real state code (no checksum needed)."""
    code = (gstin or "").strip()[:2]
    return code if code in STATE_CODES else ""


def pan_from_gstin(gstin):
    return gstin[2:12] if GSTIN_RE.match(gstin or "") else ""


def state_code_from_name(name):
    return STATE_BY_NAME.get((name or "").strip().casefold(), "")


def resolve_state_code(gstin, state_name):
    """GSTIN prefix wins; otherwise fall back to the selected state."""
    return state_code_from_gstin(gstin) or state_code_from_name(state_name)


def supply_type(seller_code, buyer_code):
    """Place of supply for services is the recipient's state, so compare the two."""
    if not seller_code or not buyer_code:
        return UNKNOWN
    if buyer_code == "96" or seller_code != buyer_code:
        return INTER_STATE            # exports / other states -> IGST
    return INTRA_UT if seller_code in UT_WITHOUT_LEGISLATURE else INTRA_STATE


def split_tax(taxable, rate, stype):
    """Return (cgst, state_or_ut_gst, igst) for one taxable amount."""
    rate = Decimal(rate)
    if stype in (INTRA_STATE, INTRA_UT):
        half = money(taxable * rate / 200)        # CGST and SGST are always equal
        return half, half, ZERO
    if stype == INTER_STATE:
        return ZERO, ZERO, money(taxable * rate / 100)
    return ZERO, ZERO, ZERO


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
         "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
         "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_100(n):
    return _ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))


def _below_1000(n):
    h, r = divmod(n, 100)
    return " ".join(p for p in (f"{_ONES[h]} Hundred" if h else "", _below_100(r) if r else "") if p)


def _indian_words(n):
    if n == 0:
        return "Zero"
    parts = []
    for div, label in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        q, n = divmod(n, div)
        if q:
            parts.append(f"{_indian_words(q)} {label}")
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)


def amount_in_words(amount):
    if amount is None:
        return ""
    rupees, paise = divmod(int(money(amount) * 100), 100)
    text = f"Indian Rupees {_indian_words(rupees)}"
    if paise:
        text += f" and {_indian_words(paise)} Paise"
    return text + " Only"