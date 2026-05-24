from __future__ import annotations

import enum


class AccountStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    BANNED = "BANNED"
    DELETED = "DELETED"


class RiskTier(str, enum.Enum):
    TRUSTED = "TRUSTED"
    STANDARD = "STANDARD"
    ELEVATED = "ELEVATED"
    HIGH_RISK = "HIGH_RISK"


class AddressType(str, enum.Enum):
    RESIDENTIAL = "RESIDENTIAL"
    COMMERCIAL = "COMMERCIAL"
    HOTEL = "HOTEL"
    STUDENT_HALL = "STUDENT_HALL"
    OTHER = "OTHER"


class DeviceType(str, enum.Enum):
    MOBILE_APP = "MOBILE_APP"
    MOBILE_WEB = "MOBILE_WEB"
    DESKTOP_WEB = "DESKTOP_WEB"
    TABLET = "TABLET"


class PaymentType(str, enum.Enum):
    CREDIT_CARD = "CREDIT_CARD"
    DEBIT_CARD = "DEBIT_CARD"
    PAYPAL = "PAYPAL"
    APPLE_PAY = "APPLE_PAY"
    GOOGLE_PAY = "GOOGLE_PAY"
    GIFT_CARD = "GIFT_CARD"
    ACCOUNT_CREDIT = "ACCOUNT_CREDIT"


class CardBrand(str, enum.Enum):
    VISA = "VISA"
    MASTERCARD = "MASTERCARD"
    AMEX = "AMEX"
    MAESTRO = "MAESTRO"
    OTHER = "OTHER"


class CardFundingType(str, enum.Enum):
    CREDIT = "CREDIT"
    DEBIT = "DEBIT"
    PREPAID = "PREPAID"


class OrderStatus(str, enum.Enum):
    PLACED = "PLACED"
    ACCEPTED = "ACCEPTED"
    READY = "READY"
    PICKED_UP = "PICKED_UP"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    FAILED = "FAILED"


class OrderChannel(str, enum.Enum):
    WEB = "WEB"
    IOS_APP = "IOS_APP"
    ANDROID_APP = "ANDROID_APP"
    IN_STORE_POS = "IN_STORE_POS"
    PHONE = "PHONE"


class OrderType(str, enum.Enum):
    DELIVERY = "DELIVERY"
    PICKUP = "PICKUP"
    DINE_IN = "DINE_IN"


class CancelledBy(str, enum.Enum):
    USER = "USER"
    MERCHANT = "MERCHANT"
    DRIVER = "DRIVER"
    SYSTEM = "SYSTEM"
    FRAUD = "FRAUD"


class FraudDecision(str, enum.Enum):
    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    DECLINE = "DECLINE"


class FraudOutcome(str, enum.Enum):
    LEGIT = "LEGIT"
    FRAUD = "FRAUD"
    CHARGEBACK = "CHARGEBACK"
    REFUND_ABUSE = "REFUND_ABUSE"
    PROMO_ABUSE = "PROMO_ABUSE"


class AVSResult(str, enum.Enum):
    MATCH = "MATCH"
    PARTIAL = "PARTIAL"
    NO_MATCH = "NO_MATCH"
    UNAVAILABLE = "UNAVAILABLE"


class CVVResult(str, enum.Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    UNAVAILABLE = "UNAVAILABLE"


class ChargebackReasonCategory(str, enum.Enum):
    FRAUD = "FRAUD"
    NOT_AS_DESCRIBED = "NOT_AS_DESCRIBED"
    NOT_RECEIVED = "NOT_RECEIVED"
    DUPLICATE = "DUPLICATE"
    OTHER = "OTHER"


class ChargebackResolution(str, enum.Enum):
    LOST = "LOST"
    WON = "WON"
    ACCEPTED = "ACCEPTED"


class ActorType(str, enum.Enum):
    USER = "USER"
    MERCHANT = "MERCHANT"
    DRIVER = "DRIVER"
    SYSTEM = "SYSTEM"


def enum_values(enum_class: type[enum.Enum]) -> list[str]:
    return [m.value for m in enum_class]
