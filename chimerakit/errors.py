"""What can go wrong, as one exception per kind of answer."""


class ChimeraError(Exception):
    """Anything the API refused or could not answer. `status` is the HTTP status, or None if no answer came. `request_id` is set on a call that made one: retry with it, never a new one."""

    def __init__(self, message, status=None, request_id=None):
        super().__init__(message)
        self.status, self.request_id = status, request_id


class AccessDenied(ChimeraError):
    """401: a wrong, revoked or expired secret, a secret without this permission, or a bank that is not switched on. The answer does not say which, on purpose."""


class LimitExceeded(ChimeraError):
    """403: over the bank's limit for one transfer or for the day."""


class NotFound(ChimeraError):
    """404: missing, or another bank's. The answer does not say which, on purpose."""


class Conflict(ChimeraError):
    """409: it cannot go ahead as things stand. A frozen account, not enough funds, a deposit already in progress, or a request ID used for something else."""


class InvalidRequest(ChimeraError):
    """422: the request itself is wrong, such as an amount that is not a positive number."""


class RateLimited(ChimeraError):
    """429, after the retries ran out."""


class Unavailable(ChimeraError):
    """No clear answer, after the retries ran out. THE CALL MAY HAVE WORKED. Retry it with `request_id` from this error, and it cannot happen twice."""


BY_STATUS = {401: AccessDenied, 403: LimitExceeded, 404: NotFound, 409: Conflict, 422: InvalidRequest, 429: RateLimited}
