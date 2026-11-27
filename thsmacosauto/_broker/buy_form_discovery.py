"""Side-aware passive discovery, separate from SELL business locator admission."""
from .form_discovery import Confirmation, DiscoveryError, _discover


def discover(backend, scope, *, confirmation=None):
    """Observe one exact unique confirmation, optionally require a closed enum.

    None auto-inspects BUY or SELL; neither choice is order direction authority.
    Public HTTP takes no selector or side. No navigation, write or retry occurs.
    """
    return _discover(backend, scope, confirmation=confirmation)
