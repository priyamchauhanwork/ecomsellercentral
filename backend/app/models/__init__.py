"""SQLAlchemy models. Import every model module here so Alembic sees all tables."""

from app.models.base import Base
from app.models.catalog import (
    Article,
    Channel,
    ChannelAccount,
    ChannelListing,
    ChannelListingItem,
    Item,
    Seller,
)
from app.models.inventory import (
    InventoryDemand,
    InventoryLog,
    InventorySupply,
    Return,
    ReturnLine,
)
from app.models.orders import (
    OrderHeader,
    OrderHistory,
    OrderImport,
    OrderImportRow,
    OrderLine,
    OrderLineHistory,
)

__all__ = [
    "Article",
    "Base",
    "Channel",
    "ChannelAccount",
    "ChannelListing",
    "ChannelListingItem",
    "InventoryDemand",
    "InventoryLog",
    "InventorySupply",
    "Item",
    "Seller",
    "OrderHeader",
    "OrderHistory",
    "OrderImport",
    "OrderImportRow",
    "OrderLine",
    "OrderLineHistory",
    "Return",
    "ReturnLine",
]
