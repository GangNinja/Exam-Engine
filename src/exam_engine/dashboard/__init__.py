"""Phase 12 local dashboard package."""

from exam_engine.dashboard.api import ApiResponse, DashboardAPI
from exam_engine.dashboard.server import create_server, serve

__all__ = ["ApiResponse", "DashboardAPI", "create_server", "serve"]
