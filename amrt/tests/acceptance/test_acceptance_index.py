"""Mandatory acceptance tests AT-01 … AT-16 (master prompt §20) → the tests that prove each one.

This index fails if any referenced test is renamed or removed, so the mapping in
docs/READINESS_REPORT.md can never silently drift from the code.
"""
import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.acceptance
T = Path(__file__).resolve().parents[1]

AT = {
    "AT-01 Specialist agents cannot place live orders": [
        "unit/test_agents.py::test_agent_modules_cannot_import_execution_or_brokers", "unit/test_execution.py::test_master_and_agents_cannot_execute",
        "unit/test_foundation.py::test_permission_matrix_invariants"],
    "AT-02 The Master Agent cannot bypass the Risk Kernel": [
        "unit/test_execution.py::test_master_and_agents_cannot_execute", "unit/test_agents.py::test_package_has_required_fields_and_proposal",
        "unit/test_agents.py::test_paper_auto_approve_routes_through_kernel"],
    "AT-03 Risk Kernel rejection cannot be overridden": [
        "unit/test_execution.py::test_kernel_rejection_cannot_be_overridden", "unit/test_execution.py::test_happy_path_and_rule_rejections"],
    "AT-04 Reliability Control Plane cannot modify financial authority": ["unit/test_reliability.py::test_supervisor_has_no_financial_authority"],
    "AT-05 Paper Mode cannot access live order APIs or credentials": [
        "unit/test_execution.py::test_paper_mode_cannot_reach_live", "unit/test_execution.py::test_venue_refuses_anything_without_a_valid_ticket",
        "security/test_security.py::test_only_the_gateway_can_hold_broker_credentials", "security/test_security.py::test_paper_app_has_no_live_route_even_with_credentials",
        "security/test_security.py::test_paper_venue_has_no_broker_imports"],
    "AT-06 Mode crossover and unauthorized live activation are blocked": [
        "unit/test_modes.py::test_paper_only_deployment_refuses_live_modes", "unit/test_modes.py::test_startup_never_restores_a_live_mode",
        "unit/test_modes.py::test_guarded_transitions"],
    "AT-07 Agent failures and disagreements produce documented safe outcomes": [
        "unit/test_agents.py::test_critical_agent_failure_gives_insufficient_data", "unit/test_agents.py::test_timeout_and_quarantine_are_contained",
        "unit/test_agents.py::test_disagreement_resolves_to_no_action", "unit/test_agents.py::test_blocking_risk_advisory_rejects_entries",
        "unit/test_agents.py::test_verification_catches_tampered_proposal", "unit/test_agents.py::test_news_injection_is_data_not_instructions",
        "chaos/test_chaos.py::test_master_failure_suspends_ai_without_promoting_another_agent"],
    "AT-08 Unknown order states remain unresolved until reconciled": [
        "unit/test_execution.py::test_ambiguous_timeout_is_unknown_and_never_duplicated", "unit/test_execution.py::test_unknown_not_at_broker_needs_repeated_reads_and_grace",
        "unit/test_execution.py::test_definitive_rejection_and_connection_errors", "chaos/test_chaos.py::test_unresolved_unknown_order_freezes_after_30s"],
    "AT-09 Duplicate orders are prevented after ambiguous timeouts": [
        "unit/test_execution.py::test_ambiguous_timeout_is_unknown_and_never_duplicated", "integration/test_postgres_redis.py::test_pg_full_live_path_with_unknown_and_duplicates"],
    "AT-10 Kill switches remain effective during agent and supervisor failures": [
        "chaos/test_chaos.py::test_kill_switch_holds_when_agents_and_supervisor_are_down", "unit/test_execution.py::test_kill_switches_block_new_risk_but_allow_exits",
        "chaos/test_chaos.py::test_database_loss_engages_switch_b", "chaos/test_chaos.py::test_watchdog_trips_on_silent_protective_loops"],
    "AT-11 Self-healing is bounded, auditable, idempotent and cannot suppress incidents": [
        "unit/test_reliability.py::test_bounded_escalation_is_audited_and_never_suppresses"],
    "AT-12 Critical service failures freeze new risk": [
        "unit/test_reliability.py::test_critical_failure_freezes_new_risk_and_sets_recovery_lock", "chaos/test_chaos.py::test_heartbeat_loss_freezes_new_risk",
        "chaos/test_chaos.py::test_stale_data_on_held_positions_freezes", "chaos/test_chaos.py::test_path_a_freezes_when_pnl_is_unavailable",
        "chaos/test_chaos.py::test_broker_read_failure_marks_reconciliation_stale", "unit/test_execution.py::test_stale_data_and_unhealthy_services_block_new_risk"],
    "AT-13 Recovery requires reconciliation and reauthorization": [
        "unit/test_reliability.py::test_critical_failure_freezes_new_risk_and_sets_recovery_lock", "e2e/test_api.py::test_csrf_step_up_and_kill_switch",
        "unit/test_execution.py::test_ambiguous_timeout_is_unknown_and_never_duplicated", "replay/test_replay_backup.py::test_decision_and_orders_replay_from_the_log"],
    "AT-14 PCR, OI, FII/DII, P&L and backtest calculations pass numerical tests": [
        "quant/test_numerics.py::test_pcr_total_and_change", "quant/test_numerics.py::test_pcr_zero_and_negative_denominators",
        "quant/test_numerics.py::test_max_and_second_oi_with_tie_breaks", "quant/test_numerics.py::test_buildup_unwinding_and_coverage",
        "quant/test_numerics.py::test_participant_oi_parse_and_nets", "quant/test_numerics.py::test_cash_parse_and_aggregation_completeness",
        "quant/test_numerics.py::test_ledger_realized_unrealized_and_missing_marks", "quant/test_backtest.py::test_straddle_pnl_is_hand_computable",
        "quant/test_backtest.py::test_fill_is_at_next_snapshot_not_the_decision_snapshot", "quant/test_backtest.py::test_iron_fly_structural_max_loss"],
    "AT-15 Disconnected states show DATA UNAVAILABLE": [
        "e2e/test_api.py::test_disconnected_market_shows_data_unavailable", "unit/test_marketdata.py::test_labels_and_staleness",
        "quant/test_numerics.py::test_unavailable_chain_has_no_numbers", "quant/test_numerics.py::test_ledger_realized_unrealized_and_missing_marks"],
    "AT-16 Security controls, secret redaction, backup restoration and rollback": [
        "unit/test_foundation.py::test_redaction_in_values_text_and_logs", "unit/test_foundation.py::test_tampering_is_detected",
        "e2e/test_api.py::test_read_only_role_cannot_act_and_denials_are_audited", "e2e/test_api.py::test_auth_required_and_security_headers",
        "security/test_security.py::test_static_hygiene", "replay/test_replay_backup.py::test_backup_restore_and_rollback",
        "replay/test_replay_backup.py::test_corrupt_backup_is_refused"],
}


def _exists(ref: str) -> bool:
    path, name = ref.split("::")
    tree = ast.parse((T / path).read_text())
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name for n in tree.body)


@pytest.mark.parametrize("at", sorted(AT))
def test_acceptance_item_is_backed_by_tests(at):
    missing = [r for r in AT[at] if not _exists(r)]
    assert not missing, missing
    assert len(AT) == 16
