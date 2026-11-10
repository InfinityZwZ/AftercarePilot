class AppError(Exception):
    """Public, sanitized application error. Never includes provider/DB exception text."""
    def __init__(self, code: str, message: str, status: int = 409, *, retryable=False, details=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.retryable, self.details = retryable, details or {}
