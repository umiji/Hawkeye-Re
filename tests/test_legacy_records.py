"""Ledger rows written before a field existed must still load.

Attack ids were introduced on 2026-07-28. Six recommendations recorded
before that had none, and because `Attack.id` is required they became
permanently unloadable — `hawkeye show` and every cross-record analysis
(`benchmark`, `drops report`, calibration) crashed on them. Recommendation
payloads are immutable (invariant 1), so the fix cannot be a backfill of the
stored JSON: the id is recomputed on the way in, from the attack's own
content, which is exactly how it was derived in the first place.
"""
from __future__ import annotations

import json

from hawkeye.contracts.models import Attack, AttackReport, attack_content_id


LEGACY_ATTACK = {
    "category": "data_integrity",
    "severity": 4,
    "statement": "The EPS beat came from a one-time tax benefit.",
    "evidence": "10-Q note 7",
    "is_kill_shot": False,
}


def test_attack_without_an_id_still_loads():
    attack = Attack.model_validate(LEGACY_ATTACK)
    assert attack.id
    assert attack.statement == LEGACY_ATTACK["statement"]


def test_recovered_id_matches_what_a_fresh_parse_would_assign():
    """Otherwise the same attack would carry two different ids depending on
    when it was recorded, and id-based judge rule checks would silently stop
    matching on old records."""
    recovered = Attack.model_validate(LEGACY_ATTACK).id
    assert recovered == attack_content_id(
        LEGACY_ATTACK["category"], LEGACY_ATTACK["statement"],
        LEGACY_ATTACK["evidence"])


def test_an_explicit_id_is_never_overwritten():
    attack = Attack.model_validate({**LEGACY_ATTACK, "id": "atk_deadbeef"})
    assert attack.id == "atk_deadbeef"


def test_two_attacks_differing_only_in_evidence_get_different_ids():
    a = Attack.model_validate(LEGACY_ATTACK)
    b = Attack.model_validate({**LEGACY_ATTACK, "evidence": "10-K note 3"})
    assert a.id != b.id


def test_a_whole_legacy_report_round_trips_from_stored_json():
    stored = json.dumps({
        "attacks": [LEGACY_ATTACK,
                    {**LEGACY_ATTACK, "category": "base_rate",
                     "statement": "Post-earnings drift is weak in this sector."}],
        "strongest_short_case": "Quality of earnings is the whole story.",
        "summary": "",
    })
    report = AttackReport.model_validate_json(stored)
    assert len(report.attacks) == 2
    assert all(a.id for a in report.attacks)
    assert report.attacks[0].id != report.attacks[1].id
