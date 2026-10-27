import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.logic.state_machine import PotholeStatus, TokenSupersededError, execute_claim
from app.logic.verification import evaluate
from tests.factories import make_event, make_pass, make_pothole


@pytest.fixture
def t0():
    return datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def pothole(t0):
    return make_pothole(
        lat=30.733300,
        lon=76.779400,
        status=PotholeStatus.CLAIMED_FIXED,
        claimed_fixed_at=t0,
    )


def test_TC_VER_01_two_clean_passes_verify(pothole, t0):
    # S1: claim -> P1 clean -> P2 clean (different pass ids) -> verified
    p1 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=1))
    p2 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=2))

    dec = evaluate(pothole, events=[], passes=[p1, p2], now=t0 + timedelta(hours=3))
    assert dec.new_status == PotholeStatus.VERIFIED
    assert dec.status_changed is True
    assert len(dec.clean_pass_ids) == 2


def test_TC_VER_02_single_clean_pass_insufficient(pothole, t0):
    # S2: claim -> P1 clean -> claimed_fixed (need 2 passes)
    p1 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=1))

    dec = evaluate(pothole, events=[], passes=[p1], now=t0 + timedelta(hours=2))
    assert dec.new_status == PotholeStatus.CLAIMED_FIXED
    assert dec.status_changed is False


def test_TC_VER_03_blurred_pass_marks_unknown(pothole, t0):
    # S3: claim -> P1 blurred -> unknown (T5)
    p1 = make_pass(
        pothole.lat,
        pothole.lon,
        crossing_time=t0 + timedelta(hours=1),
        quality={"sharpness": 10.0},
    )

    dec = evaluate(pothole, events=[], passes=[p1], now=t0 + timedelta(hours=2))
    assert dec.new_status == PotholeStatus.UNKNOWN
    assert dec.status_changed is True


def test_TC_VER_04_blurred_then_two_clean_passes_verify(pothole, t0):
    # S4: claim -> P1 blurred -> P2 clean -> P3 clean -> verified (via unknown)
    p1 = make_pass(
        pothole.lat,
        pothole.lon,
        crossing_time=t0 + timedelta(hours=1),
        quality={"sharpness": 10.0},
    )
    p2 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=2))
    p3 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=3))

    dec = evaluate(pothole, events=[], passes=[p1, p2, p3], now=t0 + timedelta(hours=4))
    assert dec.new_status == PotholeStatus.VERIFIED


def test_TC_VER_05_contradicting_event_beats_clean_passes(pothole, t0):
    # S5: claim -> P1 clean -> event at P (conf 0.8) -> reopened (T7 beats clean passes)
    p1 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=1))
    e1 = make_event(
        lat=pothole.lat,
        lon=pothole.lon,
        captured_at=t0 + timedelta(hours=2),
        confidence=0.80,
    )

    dec = evaluate(pothole, events=[e1], passes=[p1], now=t0 + timedelta(hours=3))
    assert dec.new_status == PotholeStatus.REOPENED
    assert dec.status_changed is True
    assert "after_event_ids" in dec.evidence


def test_TC_VER_06_later_detection_reopens_verified(pothole, t0):
    # S6: claim -> 2 clean passes -> verified -> later event -> reopened
    p1 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=1))
    p2 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=2))

    # Pothole is verified
    p_verified = make_pothole(
        lat=pothole.lat,
        lon=pothole.lon,
        status=PotholeStatus.VERIFIED,
        claimed_fixed_at=t0,
    )

    # Later event at t0 + 5h
    e_later = make_event(
        lat=pothole.lat,
        lon=pothole.lon,
        captured_at=t0 + timedelta(hours=5),
        confidence=0.85,
    )

    dec = evaluate(p_verified, events=[e_later], passes=[p1, p2], now=t0 + timedelta(hours=6))
    assert dec.new_status == PotholeStatus.REOPENED
    assert dec.status_changed is True


def test_TC_VER_07_pass_before_claim_ignored(pothole, t0):
    # S7: P1 clean at t1 < claim at t2 -> P2 clean -> claimed_fixed (P1 ignored)
    p1_early = make_pass(pothole.lat, pothole.lon, crossing_time=t0 - timedelta(hours=1))
    p2_post = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=1))

    dec = evaluate(pothole, events=[], passes=[p1_early, p2_post], now=t0 + timedelta(hours=2))
    assert dec.new_status == PotholeStatus.CLAIMED_FIXED
    assert dec.status_changed is False
    assert len(dec.clean_pass_ids) == 1


def test_TC_VER_08_pre_claim_event_does_not_reopen(pothole, t0):
    # S8: claim at t2; event uploaded late with captured_at < t2 -> unchanged
    e_pre = make_event(
        lat=pothole.lat,
        lon=pothole.lon,
        captured_at=t0 - timedelta(hours=1),
        confidence=0.90,
    )

    dec = evaluate(pothole, events=[e_pre], passes=[], now=t0 + timedelta(hours=1))
    assert dec.new_status == PotholeStatus.CLAIMED_FIXED
    assert dec.status_changed is False


def test_TC_VER_09_pass_crossing_twice_counts_once(pothole, t0):
    # S9: one pass crosses P twice (loop), both clean -> counts as 1 clean pass
    # Even if pass has multiple crossings or is evaluated, distinct pass ids = 1
    p_loop = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=1))

    dec = evaluate(pothole, events=[], passes=[p_loop, p_loop], now=t0 + timedelta(hours=2))
    assert dec.new_status == PotholeStatus.CLAIMED_FIXED
    assert len(dec.clean_pass_ids) == 1


def test_TC_VER_10_duplicate_pass_id_counts_once(pothole, t0):
    # S10: two passes with same pass id uploaded twice -> counts once
    same_id = uuid.uuid4()
    p1 = make_pass(pothole.lat, pothole.lon, pass_id=same_id, crossing_time=t0 + timedelta(hours=1))
    p2 = make_pass(pothole.lat, pothole.lon, pass_id=same_id, crossing_time=t0 + timedelta(hours=1))

    dec = evaluate(pothole, events=[], passes=[p1, p2], now=t0 + timedelta(hours=2))
    assert dec.new_status == PotholeStatus.CLAIMED_FIXED
    assert len(dec.clean_pass_ids) == 1


def test_TC_VER_11_out_of_order_upload_deterministic(pothole, t0):
    # S11: P2 uploaded before event that explains it -> identical outcomes regardless of order
    p1 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=1))
    p2 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=2))
    e1 = make_event(
        lat=pothole.lat,
        lon=pothole.lon,
        captured_at=t0 + timedelta(hours=3),
        confidence=0.8,
    )

    dec_order1 = evaluate(pothole, events=[e1], passes=[p1, p2], now=t0 + timedelta(hours=4))
    dec_order2 = evaluate(pothole, events=[e1], passes=[p2, p1], now=t0 + timedelta(hours=4))

    assert dec_order1.new_status == dec_order2.new_status == PotholeStatus.REOPENED
    assert dec_order1.reason == dec_order2.reason


def test_TC_VER_12_event_in_pass_invalidates_clean_and_reopens(pothole, t0):
    # S12: claim -> P1 clean but same device logged event merged into P during P1 ->
    # P1 not clean (detection_in_pass), event reopens
    dev_id = uuid.uuid4()
    p1 = make_pass(
        pothole.lat,
        pothole.lon,
        device_id=dev_id,
        crossing_time=t0 + timedelta(hours=1),
    )
    e1 = make_event(
        lat=pothole.lat,
        lon=pothole.lon,
        device_id=dev_id,
        pass_id=p1.id,
        captured_at=t0 + timedelta(hours=1),
        confidence=0.85,
    )

    dec = evaluate(pothole, events=[e1], passes=[p1], now=t0 + timedelta(hours=2))
    assert dec.new_status == PotholeStatus.REOPENED


def test_TC_VER_13_claim_when_already_claimed_fixed(pothole, t0):
    # S13: claim when already claimed_fixed -> unchanged, idempotent
    res = execute_claim(
        current_status=PotholeStatus.CLAIMED_FIXED,
        token_epoch=0,
        current_token_epoch=0,
        claim_time=t0 + timedelta(hours=1),
        existing_claimed_fixed_at=t0,
    )
    assert res.new_status == PotholeStatus.CLAIMED_FIXED
    assert res.status_changed is False
    assert res.claimed_fixed_at == t0


def test_TC_VER_14_old_link_after_reopen_returns_superseded():
    # S14: reopen -> token rotated, old link -> 410
    with pytest.raises(TokenSupersededError):
        execute_claim(
            current_status=PotholeStatus.REPORTED,
            token_epoch=0,
            current_token_epoch=1,
            claim_time=datetime.now(UTC),
        )


def test_TC_VER_15_pass_lateral_offset_12m_no_coverage(pothole, t0):
    # S15: pass at lateral offset 12 m -> no coverage row
    p_far = make_pass(
        pothole.lat,
        pothole.lon,
        lateral_offset_m=12.0,
        crossing_time=t0 + timedelta(hours=1),
    )
    dec = evaluate(pothole, events=[], passes=[p_far], now=t0 + timedelta(hours=2))
    assert dec.coverage_results == []
    assert dec.new_status == PotholeStatus.CLAIMED_FIXED


def test_TC_VER_16_pass_facing_away_insufficient_samples(pothole, t0):
    # S16: pass facing away (heading diff 120°) -> window empty
    # -> not clean, insufficient_window_samples
    p_away = make_pass(
        pothole.lat,
        pothole.lon,
        quality={"heading_deg": 120.0},
        crossing_time=t0 + timedelta(hours=1),
    )
    dec = evaluate(pothole, events=[], passes=[p_away], now=t0 + timedelta(hours=2))
    assert len(dec.coverage_results) == 1
    assert dec.coverage_results[0].clean is False
    assert dec.coverage_results[0].reason == "insufficient_window_samples"
    assert dec.new_status == PotholeStatus.UNKNOWN


def test_TC_VER_17_evaluate_pure_and_idempotent(pothole, t0):
    # TC-VER-17: evaluate same data 5 times -> same decision
    p1 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=1))
    p2 = make_pass(pothole.lat, pothole.lon, crossing_time=t0 + timedelta(hours=2))

    decisions = [
        evaluate(pothole, events=[], passes=[p1, p2], now=t0 + timedelta(hours=3)) for _ in range(5)
    ]
    for d in decisions:
        assert d.new_status == PotholeStatus.VERIFIED
        assert d.reason == decisions[0].reason
        assert d.clean_pass_ids == decisions[0].clean_pass_ids
