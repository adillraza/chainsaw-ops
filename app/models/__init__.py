"""SQLAlchemy models for chainsaw-ops.

Importing this package registers every model with the shared
:data:`app.extensions.db` metadata so Alembic's autogenerate sees them.
"""
from app.models.annotations import Annotation
from app.models.call_events import CallEvent, PinnedCall
from app.models.call_sensitivity import CallSensitivityFlag
from app.models.customer_cache import (
    CachedCallBehavior,
    CachedCallHistory,
    CachedCustomer360,
    CachedEmailMessage,
    CachedEmailRecipient,
    CachedNetoProduct,
    CachedPhoneLookup,
    CacheWatermark,
)
from app.models.internal_phone import InternalPhoneNumber
from app.models.purchase_orders import (
    CachedPurchaseOrderComparison,
    CachedPurchaseOrderItem,
    CachedPurchaseOrderSummary,
)
from app.models.reviews import (
    CLOSED_REVIEW_STATUSES,
    OPEN_REVIEW_STATUSES,
    ItemReview,
)
from app.models.shop_order import (
    CachedSeasonalityIndex,
    CachedShopOrderMsl,
    CachedShopOrderSmart,
    CachedWeatherAlert,
    CachedWeatherCurrent,
    CachedWeatherForecast,
)
from app.models.role import Role
from app.models.user import LoginLog, User

__all__ = [
    "Annotation",
    "CacheWatermark",
    "CachedCallBehavior",
    "CachedCallHistory",
    "CachedCustomer360",
    "CachedEmailMessage",
    "CachedEmailRecipient",
    "CachedNetoProduct",
    "CachedPhoneLookup",
    "CachedSeasonalityIndex",
    "CachedShopOrderMsl",
    "CachedShopOrderSmart",
    "CachedWeatherAlert",
    "CachedWeatherCurrent",
    "CachedWeatherForecast",
    "CachedPurchaseOrderComparison",
    "CachedPurchaseOrderItem",
    "CachedPurchaseOrderSummary",
    "CallEvent",
    "CallSensitivityFlag",
    "PinnedCall",
    "CLOSED_REVIEW_STATUSES",
    "InternalPhoneNumber",
    "ItemReview",
    "LoginLog",
    "OPEN_REVIEW_STATUSES",
    "Role",
    "User",
]
