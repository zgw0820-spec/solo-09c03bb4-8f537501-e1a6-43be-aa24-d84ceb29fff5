"""Domain errors mapped to HTTP responses by the API layer."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network


class ApiError(Exception):
    """Base class for errors that carry an explicit HTTP status."""

    status_code = 400
    code = "api_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class UnknownNeighbor(ApiError):
    status_code = 404
    code = "unknown_neighbor"

    def __init__(self, neighbor: IPv4Address) -> None:
        super().__init__(
            f"neighbor {neighbor} has no established BGP session; "
            "send a reconnect event first",
            neighbor=str(neighbor),
        )


class SessionConflict(ApiError):
    """A message belongs to a session that is not the neighbor's current one."""

    status_code = 409
    code = "session_conflict"

    def __init__(
        self,
        *,
        neighbor: IPv4Address,
        current_session: int,
        received_session: int,
        reason: str,
    ) -> None:
        if reason == "stale":
            code = "stale_session"
            message = (
                f"session {received_session} for neighbor {neighbor} is stale; "
                f"current session is {current_session}"
            )
        elif reason == "not_increasing":
            code = "session_not_increasing"
            message = (
                f"reconnect session {received_session} for neighbor {neighbor} "
                f"must be greater than current session {current_session}"
            )
        else:
            code = "session_not_established"
            message = (
                f"session {received_session} for neighbor {neighbor} has not been "
                f"established (current session is {current_session}); "
                "send the reconnect event first"
            )
        self.code = code
        self.reason = reason
        super().__init__(
            message,
            neighbor=str(neighbor),
            current_session=current_session,
            received_session=received_session,
            reason=reason,
        )


class EndOfRibPending(ApiError):
    """A reconciliation ticket was requested before the session converged."""

    status_code = 409
    code = "end_of_rib_pending"

    def __init__(self, neighbor: IPv4Address, session_id: int) -> None:
        super().__init__(
            f"neighbor {neighbor} session {session_id} has not completed its "
            "initial update (End-of-RIB); a reconciliation ticket can only be "
            "issued for the current session once End-of-RIB has been received",
            neighbor=str(neighbor),
            session_id=session_id,
        )


class ReconciliationTicketInvalid(ApiError):
    """The submitted reconciliation ticket is not the neighbor's active one."""

    status_code = 409
    code = "reconciliation_ticket_invalid"

    def __init__(self, neighbor: IPv4Address, ticket: str) -> None:
        super().__init__(
            f"reconciliation ticket {ticket!r} is not active for neighbor "
            f"{neighbor}; it was never issued, superseded by a newer ticket, "
            "consumed by a successful submission, or invalidated by a "
            "reconnect or reset; the online table was left unchanged",
            neighbor=str(neighbor),
            ticket=ticket,
        )


class DuplicateSnapshotPrefix(ApiError):
    """The submitted snapshot lists the same prefix more than once."""

    status_code = 400
    code = "duplicate_prefix"

    def __init__(
        self, neighbor: IPv4Address, duplicates: list[IPv4Network]
    ) -> None:
        super().__init__(
            f"snapshot for neighbor {neighbor} lists "
            f"{len(duplicates)} duplicate prefix(es): "
            + ", ".join(str(prefix) for prefix in duplicates)
            + "; the submission was rejected and the online table was "
            "left unchanged",
            neighbor=str(neighbor),
            duplicates=[str(prefix) for prefix in duplicates],
        )
