"""丰源工作台 - 数据模型包

业务模型按领域分文件组织在子模块中，这里统一 re-export 保持兼容：
    from models import ShippingOrder  # 仍然可用
"""
from ._db import get_db, DB_PATH
from ._init import init_db
from .basic import WorkLog, ErrorLog, TodoItem, VehicleMaintenance
from .notice import Notice, NoticeImage
from .orders import (
    ShippingOrder, ShippingRecord, ShippingImage,
    InboundOrder, InboundRecord, InboundImage,
    LoadingOrder, LoadingOrderRecord, LoadingOrderImage,
    UnifiedSearch, OcrMatchEvent,
)
from .stock import StockOutItem
from .products import ProductUnit, ProductCategory, Product
from .piece_conversion import PieceConversion
from .audit import AuditLog
from .audit_query import OcrEventAudit

__all__ = [
    'get_db', 'DB_PATH', 'init_db',
    'WorkLog', 'ErrorLog', 'TodoItem', 'VehicleMaintenance',
    'Notice', 'NoticeImage',
    'ShippingOrder', 'ShippingRecord', 'ShippingImage',
    'InboundOrder', 'InboundRecord', 'InboundImage',
    'LoadingOrder', 'LoadingOrderRecord', 'LoadingOrderImage',
    'UnifiedSearch', 'OcrMatchEvent',
    'StockOutItem',
    'ProductUnit', 'ProductCategory', 'Product',
    'PieceConversion',
    'AuditLog',
    'OcrEventAudit',
]
