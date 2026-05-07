"""SQLAlchemy models for chainsaw-ops.

Importing this package registers every model with the shared
:data:`app.extensions.db` metadata so Alembic's autogenerate sees them.
"""
from app.models.annotations import Annotation
from app.models.call_events import CallEvent, PinnedCall
from app.models.customer_cache import (
    CachedCallBehavior,
    CachedCallHistory,
    CachedCustomer360,
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
from app.models.role import Role
from app.models.user import LoginLog, User

__all__ = [
    "Annotation",
    "CacheWatermark",
    "CachedCallBehavior",
    "CachedCallHistory",
    "CachedCustomer360",
    "CachedNetoProduct",
    "CachedPhoneLookup",
    "CachedPurchaseOrderComparison",
    "CachedPurchaseOrderItem",
    "CachedPurchaseOrderSummary",
    "CallEvent",
    "PinnedCall",
    "CLOSED_REVIEW_STATUSES",
    "InternalPhoneNumber",
    "ItemReview",
    "LoginLog",
    "OPEN_REVIEW_STATUSES",
    "Role",
    "User",
]
