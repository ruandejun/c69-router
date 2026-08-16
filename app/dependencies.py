"""
GenRouter v2.0 — Dependencies
"""

from fastapi import Request
from app.config import load_config, AppConfig

def get_config() -> AppConfig:
    """Load configuration from disk."""
    return load_config()

def get_mac_registry(request: Request):
    """Retrieve MACRegistry from FastAPI app state."""
    return getattr(request.app.state, "mac_registry", None)

def get_clash_manager(request: Request):
    """Retrieve ClashManager from FastAPI app state."""
    return getattr(request.app.state, "clash_manager", None) or getattr(request.app.state, "singbox_manager", None)

def get_singbox_manager(request: Request):
    """Retrieve ClashManager/SingBoxManager from FastAPI app state (alias for backward compatibility)."""
    return get_clash_manager(request)

def get_dhcp_server(request: Request):
    """Retrieve DHCPServer from FastAPI app state."""
    return getattr(request.app.state, "dhcp_server", None)
