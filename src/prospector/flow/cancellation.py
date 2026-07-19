"""Cooperative cancellation signal shared by the scheduler and research runtime."""


class JobCancelledError(RuntimeError):
    """Raised when a persisted Job cancellation request reaches a safe boundary."""
