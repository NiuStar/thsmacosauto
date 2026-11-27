"""Fixed errors shared by native capabilities."""
from .engine import ServiceError


class NativeError(ServiceError):
    def __init__(self, code):
        super().__init__(code, 503)


def _block(code):
    raise NativeError(code)
