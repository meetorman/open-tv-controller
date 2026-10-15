"""Errors a client sees: an HTTP status, a stable code and a plain message."""

from __future__ import annotations


class ControllerError(Exception):
    status = 500
    code = "internal"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class BadRequest(ControllerError):
    status, code = 400, "bad_request"


class Unauthorized(ControllerError):
    status, code = 401, "unauthorized"


class Forbidden(ControllerError):
    status, code = 403, "forbidden"


class UnknownTv(ControllerError):
    status, code = 404, "unknown_tv"


class NotFound(ControllerError):
    status, code = 404, "not_found"


class NotPaired(ControllerError):
    status, code = 409, "not_paired"


class Unavailable(ControllerError):
    """Supported, but not right now (pause with nothing playing)."""

    status, code = 409, "unavailable"


class Unsupported(ControllerError):
    """The TV doesn't list the capability."""

    status, code = 422, "unsupported"


class PairingFailed(ControllerError):
    status, code = 422, "pairing_failed"


class Unreachable(ControllerError):
    status, code = 502, "tv_unreachable"


class TvTimeout(ControllerError):
    status, code = 504, "tv_timeout"
