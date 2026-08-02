"""Empirical probes for claims made in the findings report."""
from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import (
    Attack, AttackCategory, AttackReport, DecisionType, Scenario, Verdict,
    AddressedAttack,
)
from hawkeye.tribunal.pipeline import _judge_rule_check
from hawkeye.risk.sizing import build_position_plan, expected_value_pct
from hawkeye.scout.earnings import EarningsEvent, screen_events
from hawkeye.ledger.scoring import brier_score

cfg = HawkeyeConfig()
print("=" * 70)
print("PROBE 1: judge rule check — paraphrase sensitivity")
print("=" * 70)
attack = Attack(
    category=AttackCategory.VALUATION, severity=5,
    statement="The current price already implies 30% revenue growth for three "
              "consecutive years, which management has never delivered.")
attacks = AttackReport(attacks=[attack], strongest_short_case="x")

# (a) Judge echoes the statement verbatim
v_verbatim = Verdict(decision=DecisionType.BUY, conviction=0.7, rationale="",
                     addressed=[AddressedAttack(
                         attack_statement=attack.statement, response="rebutted")])
# (b) Judge paraphrases — semantically a full response to the same attack
v_para = Verdict(decision=DecisionType.BUY, conviction=0.7, rationale="",
                 addressed=[AddressedAttack(
                     attack_statement="Valuation implies 30% growth for 3 years",
                     response="rebutted: consensus already models 22%, and the "
                              "stock trades at 14x forward on that basis")])
# (c) Judge quotes it but fixes a typo / changes one word
v_near = Verdict(decision=DecisionType.BUY, conviction=0.7, rationale="",
                 addressed=[AddressedAttack(
                     attack_statement="The current price already implies ~30% revenue "
                                      "growth for three consecutive years, which "
                                      "management has never delivered.",
                     response="rebutted")])
for name, v in (("verbatim echo", v_verbatim), ("paraphrase", v_para),
                ("one char changed ('~')", v_near)):
    viol = _judge_rule_check(v, attacks)
    print(f"  {name:26s} -> violations={len(viol)}  "
          f"{'BUY survives' if not viol else 'BUY OVERTURNED to PASS'}")

print()
print("=" * 70)
print("PROBE 2: can the Bull's own numbers clear the risk hurdles?")
print("=" * 70)
entry = 100.0
# A deliberately mediocre setup: stop 8% away (the doctrine fallback).
# Bull picks scenario targets. Try an honest table vs an inflated one.
honest = [Scenario(name="bear", probability=0.35, price_target=88.0),
          Scenario(name="base", probability=0.45, price_target=104.0),
          Scenario(name="bull", probability=0.20, price_target=115.0)]
inflated = [Scenario(name="bear", probability=0.20, price_target=92.0),
            Scenario(name="base", probability=0.50, price_target=112.0),
            Scenario(name="bull", probability=0.30, price_target=130.0)]
for name, sc in (("honest scenarios", honest), ("inflated scenarios", inflated)):
    base = next(s for s in sc if s.name == "base")
    plan = build_position_plan(nav=100_000, entry_price=entry, stop_price=92.0,
                               target_price=base.price_target, scenarios=sc,
                               config=cfg)
    print(f"  {name:20s} EV={plan.expected_value_pct:+6.2f}%  "
          f"RR={plan.reward_risk:.2f}  "
          f"{'APPROVED' if plan.approved else 'vetoed: ' + plan.vetoes[0][:40]}")
print("  -> the same stock, same stop; only the Bull's own numbers changed.")

# And: how tight a stop does it take to clear RR>=2 on the honest base case?
for stop in (92.0, 95.0, 98.0):
    plan = build_position_plan(nav=100_000, entry_price=entry, stop_price=stop,
                               target_price=104.0, scenarios=honest, config=cfg)
    print(f"  stop={stop:.0f} ({(entry-stop)/entry*100:.0f}% away)  "
          f"RR={plan.reward_risk:.2f}  "
          f"{'APPROVED' if plan.approved else 'VETOED'}")

print()
print("=" * 70)
print("PROBE 3: scout ranking — does a penny-EPS beat outrank a real one?")
print("=" * 70)
from datetime import date
events = [
    EarningsEvent("TINY", date(2026, 7, 1), 0.03, 0.01, None, None),   # $0.02 beat
    EarningsEvent("MEGA", date(2026, 7, 1), 2.60, 2.20, 5.5e9, 5.0e9), # $0.40 beat
    EarningsEvent("SMOL", date(2026, 7, 1), 0.02, 0.01, None, None),   # $0.01 beat
]
for ev, eps_s, rev_s in screen_events(events, cfg.scout_min_eps_surprise_pct,
                                      cfg.scout_min_revenue_surprise_pct):
    print(f"  {ev.ticker}: EPS {ev.eps_estimate} -> {ev.eps_actual} "
          f"= {eps_s:+.0f}% surprise (absolute beat ${ev.eps_actual-ev.eps_estimate:.2f})")
print("  -> ranking is by % surprise, so the smallest absolute beats sort first.")

print()
print("=" * 70)
print("PROBE 4: do unresolved claims affect the Brier score?")
print("=" * 70)
# Bull states 5 claims. Only the 2 that came true get resolved by a busy user.
all_claims = [0.85, 0.80, 0.75, 0.70, 0.60]
truth      = [True, True, False, False, False]
resolved_all = list(zip(all_claims, truth))
resolved_selective = [(0.85, True), (0.80, True)]   # only the wins resolved
print(f"  all 5 resolved      -> Brier={brier_score(resolved_all):.3f}")
print(f"  only 2 wins resolved-> Brier={brier_score(resolved_selective):.3f}")
print("  -> unresolved claims are silently dropped; the metric is self-selected.")
