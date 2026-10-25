"""Pure state machine transitions, guards, and reason codes for CivicLens.

Implements docs/STATE_MACHINE.md.
All status transitions must be validated through this module.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PotholeStatus(StrEnum):
    OPEN = "open"
    REPORTED = "reported"
    CLAIMED_FIXED = "claimed_fixed"
    UNKNOWN = "unknown"
    VERIFIED = "verified"
    REOPENED = "reopened"


class TransitionReason(StrEnum):
    FIRST_EVENT = "first_event"
    REPORT_SENT = "report_sent"
    FIXED_CLAIM = "fixed_claim"
    CLEAN_PASSES_MET = "clean_passes_met"
    NO_VALID_CLEAN_PASS = "no_valid_clean_pass"
    CONTRADICTING_EVENT = "contradicting_event"
    REOPEN_REPORT_SENT = "reopen_report_sent"
    REMINDER_SENT = "reminder_sent"
    ESCALATION_SENT = "escalation_sent"


class IllegalTransitionError(Exception):
    """Raised when an invalid status transition is attempted."""

    def __init__(self, from_status: str | None, to_status: str, msg: str = ""):
        self.from_status = from_status
        self.to_status = to_status
        message = msg or f"Illegal transition from '{from_status}' to '{to_status}'"
        super().__init__(message)


class TokenSupersededError(Exception):
    """Raised when an out-of-date Fixed token from a previous epoch is used (HTTP 410)."""


class InvalidClaimStateError(Exception):
    """Raised when a pothole is not in a state that accepts a repair claim (HTTP 409)."""


# Allowed transitions (from_status, to_status)
ALLOWED_TRANSITIONS: set[tuple[str | None, str]] = {
    (None, PotholeStatus.OPEN),  # T1 First event
    (PotholeStatus.OPEN, PotholeStatus.REPORTED),  # T2 Initial email sent
    (PotholeStatus.REPORTED, PotholeStatus.CLAIMED_FIXED),  # T3 Fixed claim
    (PotholeStatus.CLAIMED_FIXED, PotholeStatus.VERIFIED),  # T4 Clean passes met
    (PotholeStatus.CLAIMED_FIXED, PotholeStatus.UNKNOWN),  # T5 No clean pass
    (PotholeStatus.UNKNOWN, PotholeStatus.VERIFIED),  # T6 Clean passes met
    (PotholeStatus.CLAIMED_FIXED, PotholeStatus.REOPENED),  # T7 Contradicting event
    (PotholeStatus.UNKNOWN, PotholeStatus.REOPENED),  # T7 Contradicting event
    (PotholeStatus.VERIFIED, PotholeStatus.REOPENED),  # T7 Contradicting event
    (PotholeStatus.REOPENED, PotholeStatus.REPORTED),  # T8 Reopened email sent
    (PotholeStatus.REPORTED, PotholeStatus.REPORTED),  # T9/T10 Escalation
}


def validate_transition(from_status: str | None, to_status: str) -> None:
    """Validate that transition from from_status to to_status is allowed.

    Raises IllegalTransitionError if forbidden.
    """
    # Specific disallowed checks highlighted in docs/STATE_MACHINE.md
    if from_status == PotholeStatus.OPEN and to_status == PotholeStatus.CLAIMED_FIXED:
        raise IllegalTransitionError(
            from_status,
            to_status,
            "open -> claimed_fixed is not allowed (no token exists before report)",
        )

    if to_status == PotholeStatus.VERIFIED and from_status not in {
        PotholeStatus.CLAIMED_FIXED,
        PotholeStatus.UNKNOWN,
    }:
        raise IllegalTransitionError(
            from_status, to_status, f"Cannot transition to verified from '{from_status}'"
        )

    if from_status == PotholeStatus.VERIFIED and to_status in {
        PotholeStatus.CLAIMED_FIXED,
        PotholeStatus.UNKNOWN,
    }:
        raise IllegalTransitionError(
            from_status,
            to_status,
            f"Verified potholes can only transition to reopened, not '{to_status}'",
        )

    if (from_status, to_status) not in ALLOWED_TRANSITIONS:
        raise IllegalTransitionError(from_status, to_status)


@dataclass(frozen=True)
class ClaimExecutionResult:
    new_status: str
    claimed_fixed_at: datetime
    status_changed: bool
    reason: str


def execute_claim(
    current_status: str,
    token_epoch: int,
    current_token_epoch: int,
    claim_time: datetime,
    existing_claimed_fixed_at: datetime | None = None,
) -> ClaimExecutionResult:
    """Execute claim logic per docs/STATE_MACHINE.md section 3.

    - If token is from an older epoch -> raises TokenSupersededError (410)
    - If status == 'reported' -> transitions to 'claimed_fixed', sets claimed_fixed_at
    - If status in {'claimed_fixed', 'unknown', 'verified'} -> returns 200 idempotent
    - If status in {'open', 'reopened'} -> raises InvalidClaimStateError (409)
    """
    if token_epoch < current_token_epoch:
        raise TokenSupersededError(
            "This link is out of date because the pothole was reopened after new evidence."
        )

    if current_status == PotholeStatus.REPORTED:
        validate_transition(current_status, PotholeStatus.CLAIMED_FIXED)
        return ClaimExecutionResult(
            new_status=PotholeStatus.CLAIMED_FIXED,
            claimed_fixed_at=claim_time,
            status_changed=True,
            reason=TransitionReason.FIXED_CLAIM,
        )

    if current_status in {
        PotholeStatus.CLAIMED_FIXED,
        PotholeStatus.UNKNOWN,
        PotholeStatus.VERIFIED,
    }:
        # Idempotent: returns current state unchanged, retains existing claimed_fixed_at
        return ClaimExecutionResult(
            new_status=current_status,
            claimed_fixed_at=existing_claimed_fixed_at or claim_time,
            status_changed=False,
            reason=TransitionReason.FIXED_CLAIM,
        )

    raise InvalidClaimStateError(
        f"Pothole status '{current_status}' does not accept repair claims."
    )


def get_claim_preview_info(
    status: str,
    token_epoch: int,
    current_token_epoch: int,
) -> tuple[bool, str]:
    """Returns (can_claim, explanation_message) for GET /v1/fixed/{token}."""
    if token_epoch < current_token_epoch:
        raise TokenSupersededError(
            "This link is out of date because the pothole was reopened after new evidence."
        )

    if status == PotholeStatus.REPORTED:
        return True, "Ready to claim"

    if status == PotholeStatus.CLAIMED_FIXED:
        return False, "This pothole has already been claimed as fixed."

    if status == PotholeStatus.UNKNOWN:
        return False, "This pothole was claimed as fixed, but drives could not confirm it."

    if status == PotholeStatus.VERIFIED:
        return False, "This repair has already been verified by subsequent drive passes."

    if status == PotholeStatus.REOPENED:
        return False, "This pothole was reopened following new sightings."

    return False, f"Pothole status '{status}' cannot be claimed."
