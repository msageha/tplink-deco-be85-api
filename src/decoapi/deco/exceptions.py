class DecoError(Exception):
    """Generic error talking to the Deco router."""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class DecoAuthError(DecoError):
    """Authentication / login failure."""


class DecoConnectionError(DecoError):
    """Network level failure reaching the router."""
