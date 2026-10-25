from datetime import UTC, datetime

import pytest

from app.logic.state_machine import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    InvalidClaimStateError,
    PotholeStatus,
    TokenSupersededError,
    TransitionReason,
    execute_claim,
    get_claim_preview_info,
    validate_transition,
)


def test_allowed_transitions():
    for from_s, to_s in ALLOWED_TRANSITIONS:
        # Should not raise
        validate_transition(from_s, to_s)


def test_forbidden_transitions_raise_error():
    forbidden = [
        (PotholeStatus.OPEN, PotholeStatus.CLAIMED_FIXED),
        (PotholeStatus.OPEN, PotholeStatus.VERIFIED),
        (PotholeStatus.REPORTED, PotholeStatus.VERIFIED),
        (PotholeStatus.VERIFIED, PotholeStatus.CLAIMED_FIXED),
        (PotholeStatus.VERIFIED, PotholeStatus.UNKNOWN),
        (PotholeStatus.VERIFIED, PotholeStatus.OPEN),
        (PotholeStatus.UNKNOWN, PotholeStatus.CLAIMED_FIXED),
        (PotholeStatus.REOPENED, PotholeStatus.VERIFIED),
        (None, PotholeStatus.VERIFIED),
    ]
    for from_s, to_s in forbidden:
        with pytest.raises(IllegalTransitionError):
            validate_transition(from_s, to_s)


def test_execute_claim_on_reported_status():
    t_claim = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    result = execute_claim(
        current_status=PotholeStatus.REPORTED,
        token_epoch=0,
        current_token_epoch=0,
        claim_time=t_claim,
    )
    assert result.new_status == PotholeStatus.CLAIMED_FIXED
    assert result.status_changed is True
    assert result.claimed_fixed_at == t_claim
    assert result.reason == TransitionReason.FIXED_CLAIM


def test_execute_claim_idempotent():
    t_original = datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)
    t_repeat = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

    for st in [
        PotholeStatus.CLAIMED_FIXED,
        PotholeStatus.UNKNOWN,
        PotholeStatus.VERIFIED,
    ]:
        result = execute_claim(
            current_status=st,
            token_epoch=0,
            current_token_epoch=0,
            claim_time=t_repeat,
            existing_claimed_fixed_at=t_original,
        )
        assert result.new_status == st
        assert result.status_changed is False
        assert result.claimed_fixed_at == t_original


def test_execute_claim_superseded_token():
    t_claim = datetime.now(UTC)
    with pytest.raises(TokenSupersededError):
        execute_claim(
            current_status=PotholeStatus.REPORTED,
            token_epoch=0,
            current_token_epoch=1,  # bumped epoch
            claim_time=t_claim,
        )


def test_execute_claim_invalid_status():
    t_claim = datetime.now(UTC)
    for st in [PotholeStatus.OPEN, PotholeStatus.REOPENED]:
        with pytest.raises(InvalidClaimStateError):
            execute_claim(
                current_status=st,
                token_epoch=0,
                current_token_epoch=0,
                claim_time=t_claim,
            )


def test_get_claim_preview_info():
    can_claim, msg = get_claim_preview_info(PotholeStatus.REPORTED, 0, 0)
    assert can_claim is True
    assert "Ready" in msg

    can_claim, msg = get_claim_preview_info(PotholeStatus.CLAIMED_FIXED, 0, 0)
    assert can_claim is False

    can_claim, msg = get_claim_preview_info(PotholeStatus.VERIFIED, 0, 0)
    assert can_claim is False

    with pytest.raises(TokenSupersededError):
        get_claim_preview_info(PotholeStatus.REPORTED, 0, 1)
