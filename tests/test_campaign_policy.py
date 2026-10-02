"""The campaign's rules as pure functions: the user's 2026-09-30 decisions, pinned.

The disposition reader is tested hardest, because it is the whole of the runner's side of the shared
contract with Interactive: the runner reads campaign_disposition and decides nothing itself.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes  # noqa: E402,F401  (puts scripts/ on the path)
from campaign import policy  # noqa: E402

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
SHA = "a" * 64


def disposition(**overrides):
    record = {
        "schema": "msdial-campaign-disposition.v1", "disposition": "run", "reasons": [], "warnings": ["aif_ce_targets_empty"],
        "excluded_inputs": [{"path": "raw/data/X.d", "reason": "ion_mobility"}], "split_key": None,
        "decided_at": "2026-10-01T00:00:00+00:00",
        "extractor": {"sha256": SHA, "inventory_sha256": "b" * 64, "provenance_status": "verified", "pinned": True},
    }
    record.update(overrides)
    return {"campaign_disposition": record}


class DispositionTests(unittest.TestCase):
    def test_a_well_formed_record_is_read_as_it_is(self) -> None:
        read = policy.read_disposition(disposition())
        self.assertEqual(read.disposition, "run")
        self.assertEqual(read.warnings, ("aif_ce_targets_empty",))
        self.assertEqual(read.excluded_inputs, ({"path": "raw/data/X.d", "reason": "ion_mobility"},))
        self.assertEqual(read.extractor["sha256"], SHA)
        self.assertEqual(policy.read_disposition(disposition(disposition="split", split_key={"acquisition": "DDA"})).split_key,
                         {"acquisition": "DDA"})

    def test_only_a_disposition_interactive_applied_reads_as_applied(self) -> None:
        self.assertTrue(policy.read_disposition(disposition(applied=True)).applied)
        self.assertFalse(policy.read_disposition(disposition(applied=False)).applied)
        self.assertFalse(policy.read_disposition(disposition()).applied, "written before 0.5.17: advice only")
        self.assertIs(policy.read_disposition(disposition(applied=True)).as_dict()["applied"], True)

    def test_no_record_is_none_not_a_decision(self) -> None:
        self.assertIsNone(policy.read_disposition({"status": "preflight_passed"}))

    def test_every_malformed_record_is_a_contract_error(self) -> None:
        cases = {
            "schema": disposition(schema="msdial-campaign-disposition.v0"),
            "disposition": disposition(disposition="maybe"),
            "reasons": disposition(reasons="acquisition_unknown"),
            "skip without a reason": disposition(disposition="skip", reasons=[]),
            "exclude without a reason": disposition(disposition="exclude", reasons=[]),
            "extractor": disposition(extractor={"sha256": "short"}),
            "split key": disposition(split_key=["DDA"]),
            "excluded input": disposition(excluded_inputs=[{"reason": "x"}]),
            "applied": disposition(applied="yes"),
        }
        for name, manifest in cases.items():
            with self.subTest(name), self.assertRaises(policy.DispositionError):
                policy.read_disposition(manifest)


class RetryTests(unittest.TestCase):
    def test_a_failed_unit_is_retried_twice_then_fails(self) -> None:
        rules = policy.CampaignPolicy()
        first = policy.after_failure(0, NOW, rules)
        second = policy.after_failure(1, NOW, rules)
        third = policy.after_failure(2, NOW, rules)
        self.assertEqual((first.state, first.failures), ("waiting_retry", 1))
        self.assertEqual(policy.parse_iso(first.next_attempt_at), NOW + timedelta(seconds=600))
        self.assertEqual((second.state, second.failures), ("waiting_retry", 2))
        self.assertEqual(policy.parse_iso(second.next_attempt_at), NOW + timedelta(seconds=3600))
        self.assertEqual((third.state, third.failures, third.next_attempt_at), ("failed", 3, None))

    def test_interruptions_count_only_past_their_budget(self) -> None:
        rules = policy.CampaignPolicy(max_interruptions=2)
        self.assertFalse(policy.interruption_counts(0, rules))
        self.assertFalse(policy.interruption_counts(1, rules))
        self.assertTrue(policy.interruption_counts(2, rules))

    def test_results_are_read_by_reason(self) -> None:
        self.assertEqual(policy.classify_result({"started": True}), policy.OK)
        self.assertEqual(policy.classify_result({"ok": False, "reason": "campaign_authorization_refused"}), policy.REFUSED)
        self.assertEqual(policy.classify_result({"ok": False, "reason": "unit_busy"}), policy.BUSY)
        self.assertEqual(policy.classify_result({"ok": False, "reason": "manifest_busy"}), policy.BUSY)
        self.assertEqual(policy.classify_result({"ok": False, "reason": "backend_unavailable"}), policy.FAULT)
        self.assertEqual(policy.classify_result({"ok": False, "reason": "validation_error"}), policy.FAILED)
        self.assertEqual(policy.classify_result("not a mapping"), policy.FAILED)
        for reason in ("unsupported", "malformed", "raw_metadata_extractor_refused"):
            with self.subTest(reason):
                self.assertEqual(policy.classify_result({"ok": False, "reason": reason}), policy.CONTRACT,
                                 "the campaign's refusal, which would recur for every unit")

    def test_the_policy_refuses_what_would_break_the_rules(self) -> None:
        with self.assertRaises(ValueError):
            policy.CampaignPolicy.from_dict({"gate_points": ["final"]})
        # A unit runs only on a before-production report (2026-10-02): without the point, every unit is held.
        with self.assertRaisesRegex(ValueError, "before_production"):
            policy.CampaignPolicy.from_dict({"gate_points": ["pre_cleanup", "final"]})
        with self.assertRaises(ValueError):
            policy.CampaignPolicy.from_dict({"max_attempts": 5})
        with self.assertRaises(ValueError):
            policy.CampaignPolicy.from_dict({"outer_extractor_timeout": 10})
        with self.assertRaises(ValueError):
            policy.CampaignPolicy.from_dict({"outage_units": 1})
        rules = policy.CampaignPolicy.from_dict({"prefetch": 1, "disk": {"reserve_bytes": 5}})
        self.assertEqual((rules.prefetch, rules.disk.reserve_bytes), (1, 5))
        self.assertEqual(policy.CampaignPolicy.from_dict(rules.as_dict()), rules)

    def test_there_is_no_outer_limit_by_default(self) -> None:
        rules = policy.CampaignPolicy()
        self.assertEqual(rules.console_timeout_seconds, 0.0)
        self.assertEqual(rules.diagnostic_timeout_seconds, 0.0)
        self.assertGreater(rules.console_idle_timeout_seconds, 0)
        self.assertEqual(rules.prefetch, 0)
        self.assertFalse(any("extractor" in name for name in rules.as_dict()))


class StallTests(unittest.TestCase):
    def test_a_download_is_stalled_only_while_fetching(self) -> None:
        last = NOW - timedelta(seconds=1801)
        self.assertTrue(policy.download_stalled(last, NOW, 1800, fetch_completed=False))
        self.assertFalse(policy.download_stalled(last, NOW, 1800, fetch_completed=True))
        self.assertFalse(policy.download_stalled(NOW - timedelta(seconds=10), NOW, 1800, fetch_completed=False))
        self.assertFalse(policy.download_stalled(last, NOW, 0, fetch_completed=False))
        manifest = {"lease_stages": [{"stage": "fetch", "status": "completed"}, {"stage": "extract", "status": "running"}]}
        self.assertTrue(policy.fetch_completed(manifest))
        self.assertFalse(policy.fetch_completed({"lease_stages": [{"stage": "fetch", "status": "running"}]}))


class DiagnosticTests(unittest.TestCase):
    def test_the_threshold_step(self) -> None:
        self.assertEqual(policy.threshold_step("Agilent 6545 Q-TOF", "QTOF"), 100)
        self.assertEqual(policy.threshold_step("", "Fourier-transform MS"), 1000)
        self.assertEqual(policy.threshold_step("Thermo Q Exactive HF-X", "QTOF"), 1000, "an Orbitrap published as mzML")
        self.assertEqual(policy.threshold_step("Orbitrap Exploris 480", ""), 1000)
        self.assertEqual(policy.threshold_step("Bruker solariX", ""), 1000)
        self.assertEqual(policy.threshold_step("Unknown", "", thermo_raw_inputs=3), 1000)
        self.assertEqual(policy.threshold_step("Waters Xevo G2-XS", ""), 100)
        # The Catalog's own FT spellings that named no Q or no Orbitrap (review finding 15).
        for instrument in ("Thermo Scientific Exactive", "Exactive Plus", "Exactive HF-X", "IQ-X tribrid",
                           "Bruker APEX-Qe 9.4T"):
            with self.subTest(instrument):
                self.assertEqual(policy.threshold_step(instrument, "QTOF"), 1000)
        for instrument in ("SCIEX TripleTOF 6600", "Agilent 6546 LC/Q-TOF", "Waters Synapt G2-Si", "Bruker impact II"):
            with self.subTest(instrument):
                self.assertEqual(policy.threshold_step(instrument, "QTOF"), 100)


class DiskTests(unittest.TestCase):
    def test_the_measured_factor_covers_the_intermediates(self) -> None:
        rules = policy.DiskPolicy()
        # MTBLS2207: 615 MB of mzML and 345 MB of MS-DIAL containers beside it.
        self.assertGreaterEqual(rules.file_factor, (615 + 345) / 615)
        self.assertEqual(policy.disk_need(1000, True, False, rules), 1660)
        self.assertEqual(policy.disk_need(1000, True, True, rules), 2660)

    def test_an_unknown_size_reserves_room(self) -> None:
        rules = policy.DiskPolicy(unknown_size_reserve_bytes=500)
        self.assertEqual(policy.disk_need(0, False, False, rules), 500)
        self.assertEqual(policy.disk_need(1000, False, False, rules), 2160)
        # A lower bound is at least what has already arrived; a declared size is the size.
        self.assertEqual(policy.disk_need(0, False, False, rules, held_bytes=1000), 2160)
        self.assertEqual(policy.disk_need(1000, True, False, rules, held_bytes=5000), 1660)

    def test_observed_units_raise_the_factor_never_lower_it(self) -> None:
        rules = policy.DiskPolicy(observed_minimum_units=3)
        self.assertIsNone(policy.observed_factor([2.0, 3.0], rules))
        self.assertEqual(policy.disk_need(1000, True, False, rules, [3.0, 3.0, 3.0]), 3000)
        self.assertEqual(policy.disk_need(1000, True, False, rules, [1.1, 1.1, 1.1]), 1660)

    def test_the_verdict(self) -> None:
        rules = policy.DiskPolicy(reserve_bytes=100, reserve_fraction=0.0)
        self.assertTrue(policy.disk_verdict(400, 500, 1000, rules).admit)
        short = policy.disk_verdict(401, 500, 1000, rules)
        self.assertFalse(short.admit)
        self.assertFalse(short.never_fits, "short now is not too big ever")
        self.assertTrue(policy.disk_verdict(901, 1000, 1000, rules).never_fits)
        # A partial download already on the volume is credited: the free space already lacks it.
        self.assertTrue(policy.disk_verdict(600, 500, 1000, rules, held=200).admit)
        self.assertFalse(policy.disk_verdict(600, 500, 1000, rules, held=199).admit)
        self.assertTrue(policy.disk_verdict(901, 1000, 1000, rules, held=900).never_fits, "its whole need, ever")
        # What the volume could hold above its reserve, whatever is free now: the floor pauses a short disk.
        self.assertEqual(policy.download_bound_gb(1000 * 1000**3, rules), (1000 * 1000**3 - 100) / 1000**3)
        self.assertEqual(policy.download_bound_gb(1000, rules), 1.0)


class DownloadFailureTests(unittest.TestCase):
    def test_the_lease_limit_is_read_from_interactives_own_words(self) -> None:
        self.assertEqual(policy.lease_size_limit({"error": "Remote object is 9000 bytes; limit is 500 bytes."}), 9000)
        self.assertEqual(policy.lease_size_limit({"error": "Download exceeded the 500-byte safety limit."}), 501)
        self.assertEqual(policy.lease_size_limit({"failure": {"reason": (
            "Required repository bundle is 7000 bytes; the download lease limit is 500 bytes.")}}), 7000)
        self.assertIsNone(policy.lease_size_limit({"error": "HTTP Error 503: Service Unavailable"}))

    def test_a_network_failure_is_told_from_the_units_own(self) -> None:
        for detail in (
            {"error": "HTTP 503"}, {"error": "HTTP Error 502: Bad Gateway"}, {"error": "HTTP Error 429: Too Many Requests"},
            {"failure": {"reason": "<urlopen error [Errno 11001] getaddrinfo failed>", "error_type": "URLError"}},
            {"error": "The read operation timed out"}, {"failure": {"error_type": "ConnectionResetError", "reason": "x"}},
            # Interactive 0.5.18 retries a stalled or lost transfer three times, then records what ended it.
            {"failure": {"reason": "No bytes arrived from the server for 120 s.", "error_type": "DownloadStalled"}},
            {"failure": {"reason": "The connection was lost", "error_type": "DownloadConnectionLost", "retryable": True}},
            {"failure": {"reason": "short body", "error_type": "DownloadIncomplete"}},
            # Whatever the error is called: Interactive marks every transfer it retried and gave up on.
            {"failure": {"reason": "the transfer was interrupted", "error_type": "DownloadInterrupted", "retryable": True}},
        ):
            with self.subTest(detail):
                self.assertTrue(policy.network_failure(detail))
        self.assertTrue(policy.network_failure({"error": "cancelled"}, "stalled"), "bytes that stop are the network's")
        # The job's error alone, as Interactive 0.5.18 leaves it: str(error) of the error that ended the lease.
        for text in (
            "No bytes arrived from the server for 120 s. The partial file is kept and a later attempt resumes it.",
            "The connection was lost during the transfer (OSError: [WinError 10054] An existing connection was "
            "dropped). The partial file is kept and a later attempt resumes it.",
            "Download ended at 5000 of 9000 declared bytes. The partial file is kept at S1.mzML.part and the next "
            "attempt will resume.",
        ):
            with self.subTest(text):
                self.assertTrue(policy.network_failure({"job_id": "dl1", "status": "failed", "error": text, "stop_reason": None}))
        for detail in (
            {"error": "HTTP Error 404: Not Found"}, {"error": "Checksum mismatch for S1.mzML"},
            {"failure": {"reason": "archive_member_escapes", "error_type": "ArchiveError"}},
        ):
            with self.subTest(detail):
                self.assertFalse(policy.network_failure(detail))

    def test_a_volume_that_ran_short_while_an_archive_expanded_is_read_as_a_short_disk(self) -> None:
        listing = "ST1.zip expands to 4000 bytes; 3000 are free and 20 are held in reserve."
        record = {"failure": {"reason": listing, "error_type": "ArchiveError", "archive_failure": {
            "reason": "insufficient_disk_space", "message": listing, "detail": {"declared_bytes": 4000, "free_bytes": 3000}}}}
        self.assertEqual(policy.extraction_disk_short(record), 4000)
        self.assertEqual(policy.extraction_disk_short({"error": listing}), 4000, "the job's words alone")
        for text in ("Free space fell below the 20000000000-byte reserve while ST1.tar.gz expanded.",
                     "7-Zip was stopped while extracting ST1.7z: insufficient_disk_space."):
            with self.subTest(text):
                self.assertEqual(policy.extraction_disk_short({"error": text}), 0, "short, by an unstated amount")
        self.assertEqual(policy.extraction_disk_short(
            {"failure": {"reason": "x", "archive_failure": {"reason": "insufficient_disk_space"}}}), 0)
        for detail in ({"failure": {"reason": "archive_member_escapes", "error_type": "ArchiveError",
                                    "archive_failure": {"reason": "archive_member_escapes"}}},
                       {"error": "HTTP Error 503: Service Unavailable"}):
            with self.subTest(detail):
                self.assertIsNone(policy.extraction_disk_short(detail))
        self.assertFalse(policy.network_failure(record), "nor is it the network's")
        self.assertEqual(policy.known_bytes_for_need(2661, True, policy.DiskPolicy()), 1001)

    def test_a_discard_blocked_for_good_is_not_waited_for(self) -> None:
        self.assertTrue(policy.discard_blocked_for_good(["mztab_output_exists"]))
        self.assertTrue(policy.discard_blocked_for_good(["finalisation_held", "validated_status"]))
        self.assertFalse(policy.discard_blocked_for_good(["lease_live", "finalisation_held"]))
        self.assertFalse(policy.discard_blocked_for_good([]))


class RunPolicyTests(unittest.TestCase):
    """A before-production FAIL stops the run only for a check that breaks results (2026-10-01)."""

    @staticmethod
    def report(*checks, **top):
        return {"checks": [dict(check) for check in checks], **top}

    def test_without_a_stated_policy_the_fixed_list_decides(self) -> None:
        report = self.report(*({"check_id": check, "status": "fail"} for check in (
            "ELIG-1", "ACQ-1", "SUM-1", "CNT-1", "INP-1", "ID-1", "PRE-2", "CONV-1",
            "CLS-1", "CLS-2", "CLS-3", "ORD-1", "PKH-1", "SPL-1")))
        self.assertEqual(policy.run_blocking_failures(report), (sorted(policy.BLOCKS_RUN_CHECKS), "runner_default"))

    def test_the_users_list_is_the_eight_checks_of_2026_10_02(self) -> None:
        self.assertEqual(set(policy.BLOCKS_RUN_CHECKS),
                         {"ELIG-1", "ACQ-1", "SUM-1", "CNT-1", "INP-1", "ID-1", "PRE-2", "CONV-1"})
        for check in ("ID-1", "PRE-2", "CONV-1"):
            with self.subTest(check=check):
                report = self.report({"check_id": check, "status": "fail", "run_policy": "record_only"})
                self.assertEqual(policy.run_blocking_failures(report), ([check], "gate"),
                                 "a gate that still states record_only for it does not take it off the list")
                self.assertEqual(len(policy.run_policy_mismatches(report)), 1)

    def test_a_blocking_check_left_unevaluated_where_required_stops_the_run(self) -> None:
        """2026-10-02: as its FAIL does. Required is the check's own word, or strict_failures where it gives none."""
        report = self.report(
            {"check_id": "SUM-1", "stage": "before-production", "status": "not_evaluable", "required": True,
             "run_policy": "blocks_run"},
            {"check_id": "INP-1", "stage": "before-production", "status": "not_evaluable", "required": False,
             "run_policy": "blocks_run"},
            {"check_id": "PKH-1", "stage": "before-production", "status": "not_evaluable", "required": True,
             "run_policy": "record_only"},
            {"check_id": "CONV-1", "status": "not_evaluable"},
            {"check_id": "ID-1", "status": "not_evaluable"},
            {"check_id": "CNT-1", "stage": "after-run", "status": "not_evaluable", "required": True},
            strict_failures=["SUM-1", "PKH-1", "CONV-1", "CNT-1"],
        )
        self.assertEqual(policy.run_blocking_unevaluated(report), ["CONV-1", "SUM-1"])
        self.assertEqual(policy.run_blocking_failures(report)[0], [], "none of them FAILed")
        named = self.report({"check_id": "ACQ-1", "stage": "before-production", "status": "not_evaluable",
                             "required": True, "run_policy": "blocks_run"}, run_blocked_by=["ACQ-1"])
        self.assertEqual(policy.run_blocking_unevaluated(named), ["ACQ-1"])
        self.assertEqual(policy.run_blocking_failures(named)[0], [], "run_blocked_by names it, and it is not evaluable")
        stated = self.report({"check_id": "CLS-1", "status": "not_evaluable", "required": True, "run_policy": "blocks_run"})
        self.assertEqual(policy.run_blocking_unevaluated(stated), ["CLS-1"], "the gate can add to the list here too")

    def test_a_gate_that_gave_no_usable_report_is_told_from_one_that_did(self) -> None:
        usable = {"outcome": "ran", "exit_code": 2, "report_parsed": True}
        self.assertIsNone(policy.gate_report_problem(usable))
        for exit_code in (0, 4):
            self.assertIsNone(policy.gate_report_problem({**usable, "exit_code": exit_code}))
        cases = {
            policy.GATE_NOT_RUN: [None, {"outcome": "error", "not_started": True}],
            policy.GATE_TIMEOUT: [{"outcome": "timeout"}],
            policy.GATE_ERROR: [{"outcome": "error", "exit_code": None}, {**usable, "exit_code": 1},
                                {**usable, "exit_code": None}, {**usable, "exit_code": True}],
            policy.GATE_UNUSABLE: [{**usable, "exit_code": 3}, {"outcome": "ran", "exit_code": 3, "report_parsed": False}],
            policy.GATE_UNPARSABLE: [{**usable, "report_parsed": False}, {"outcome": "ran", "exit_code": 0}],
        }
        for problem, verdicts in cases.items():
            for verdict in verdicts:
                with self.subTest(verdict=verdict):
                    self.assertEqual(policy.gate_report_problem(verdict), problem)

    def test_only_a_fail_blocks(self) -> None:
        report = self.report({"check_id": "SUM-1", "status": "warn"}, {"check_id": "INP-1", "status": "not_evaluable"},
                             {"check_id": "ELIG-1", "status": "pass"}, {"check_id": "ACQ-1", "status": "FAIL"})
        self.assertEqual(policy.run_blocking_failures(report), (["ACQ-1"], "runner_default"))

    def test_the_gate_can_add_to_the_users_list_and_never_take_from_it(self) -> None:
        per_check = self.report({"check_id": "SUM-1", "status": "fail", "run_policy": "record_only"},
                                {"check_id": "CLS-1", "status": "fail", "run_policy": "blocks_run"},
                                {"check_id": "ORD-1", "status": "fail"})
        self.assertEqual(policy.run_blocking_failures(per_check), (["CLS-1", "SUM-1"], "gate"))
        self.assertEqual(len(policy.run_policy_mismatches(per_check)), 1, "SUM-1's record_only is the gate's mismatch")
        by_list = self.report({"check_id": "SUM-1", "status": "fail"}, {"check_id": "PKH-1", "status": "fail"},
                              {"check_id": "ORD-1", "status": "fail"},
                              run_policy={"blocks_run": ["PKH-1"], "record_only": ["SUM-1", "ORD-1"]})
        self.assertEqual(policy.run_blocking_failures(by_list), (["PKH-1", "SUM-1"], "gate"))
        by_check = self.report({"check_id": "INP-1", "status": "fail"}, run_policy={"INP-1": "record_only"})
        self.assertEqual(policy.run_blocking_failures(by_check), (["INP-1"], "gate"))
        self.assertEqual(policy.run_policy_mismatches(self.report({"check_id": "ORD-1", "status": "fail", "run_policy": "record_only"})),
                         [], "a record_only the user's rule agrees with")

    def test_a_policy_stated_for_some_checks_keeps_the_list_for_the_others(self) -> None:
        report = self.report({"check_id": "ELIG-1", "status": "fail"},
                             {"check_id": "CLS-1", "status": "pass", "run_policy": "record_only"})
        self.assertEqual(policy.run_blocking_failures(report), (["ELIG-1"], "gate"))

    def test_a_rule_or_a_shape_this_reader_does_not_know_blocks_and_is_said(self) -> None:
        for value in ("BLOCKS_RUN", "blocks-run", {"campaign": "blocks_run"}, True, "block"):
            with self.subTest(value=value):
                report = self.report({"check_id": "ELIG-1", "status": "fail", "run_policy": value},
                                     {"check_id": "CLS-1", "status": "fail", "run_policy": value})
                self.assertEqual(policy.run_blocking_failures(report)[0], ["CLS-1", "ELIG-1"])
                self.assertEqual(len(policy.run_policy_mismatches(report)), 2)
        one_id = self.report({"check_id": "ELIG-1", "status": "fail"}, {"check_id": "CLS-1", "status": "fail"},
                             run_policy={"blocks_run": "CLS-1"})
        self.assertEqual(policy.run_blocking_failures(one_id), (["CLS-1", "ELIG-1"], "gate"), "one check id, not a list")
        self.assertEqual(policy.run_policy_mismatches(one_id), [])
        for top in ([["ELIG-1"]], "blocks_run", {"blocks_run": 5}, {"version": 1, "record_only": ["ELIG-1"]}):
            with self.subTest(top=top):
                report = self.report({"check_id": "ELIG-1", "status": "fail"}, {"check_id": "ORD-1", "status": "fail"},
                                     run_policy=top)
                self.assertEqual(policy.run_blocking_failures(report), (["ELIG-1"], "gate"))
                self.assertTrue(policy.run_policy_mismatches(report), "said, so the contract can be mended")

    def test_a_check_of_a_later_stage_has_no_run_to_stop(self) -> None:
        """The gate's report as feat/alias-aware-checks-and-run-policy writes it: each before-production check
        states blocks_run or record_only, a check of a later stage states null, and run_blocked_by lists the
        FAILs it reads as blocks_run. A --stage all report (pre_cleanup, final) holds both kinds, and CNT-1
        once for each stage."""
        report = self.report(
            {"check_id": "ELIG-1", "stage": "before-production", "status": "pass", "run_policy": "blocks_run"},
            {"check_id": "CLS-1", "stage": "before-production", "status": "fail", "run_policy": "record_only"},
            {"check_id": "CNT-1", "stage": "before-production", "status": "pass", "run_policy": "blocks_run"},
            {"check_id": "CNT-1", "stage": "after-run", "status": "fail", "run_policy": None},
            {"check_id": "TAB-1", "stage": "after-run", "status": "fail", "run_policy": None},
            {"check_id": "SEC-1", "stage": "before-publish", "status": "fail", "run_policy": None},
            run_blocked_by=[],
        )
        self.assertEqual(policy.run_blocking_failures(report), ([], "gate"), "a FAIL after production stops no run")
        self.assertEqual(policy.run_policy_mismatches(report), [], "null is the gate's word for a later stage")
        # A before-production check the gate's table leaves out is decided by the user's list, and said.
        gap = self.report({"check_id": "NEW-1", "stage": "before-production", "status": "fail", "run_policy": None},
                          {"check_id": "INP-1", "stage": "before-production", "status": "fail", "run_policy": None})
        self.assertEqual(policy.run_blocking_failures(gap), (["INP-1"], "gate"))
        self.assertEqual(len(policy.run_policy_mismatches(gap)), 2)
        # The gate's run_blocked_by adds to the list; a shape that is not a list of ids is said.
        named = self.report({"check_id": "PRE-2", "stage": "before-production", "status": "fail", "run_policy": "record_only"},
                            run_blocked_by=["PRE-2"])
        self.assertEqual(policy.run_blocking_failures(named), (["PRE-2"], "gate"))
        self.assertEqual(policy.run_blocking_failures(self.report(run_blocked_by="CLS-1")), (["CLS-1"], "gate"), "one id")
        self.assertTrue(policy.run_policy_mismatches(self.report(run_blocked_by={"ELIG-1": True})))


class OutputsTests(unittest.TestCase):
    def test_outputs_produced_is_the_validated_manifest(self) -> None:
        self.assertTrue(policy.outputs_produced({"status": "mztab_validated", "cleanup_allowed": True}))
        self.assertFalse(policy.outputs_produced({"status": "validation_failed", "cleanup_allowed": False}))
        self.assertFalse(policy.outputs_produced({"status": "mztab_validated"}))

    def test_two_terms(self) -> None:
        self.assertEqual(policy.report_terms(True, 0), [policy.OUTPUTS_PRODUCED, policy.COMPLETED])
        self.assertEqual(policy.report_terms(True, 4), [policy.OUTPUTS_PRODUCED], "READ-1 holds exit 4: not completed")
        self.assertEqual(policy.report_terms(False, 0), [])
        self.assertEqual(policy.gate_verdict_token(4), "held")


class PinTests(unittest.TestCase):
    def test_every_pinned_identity_is_compared(self) -> None:
        recorded = {
            "console": {"path": "C:/a/MSDIALCUI.exe", "binary_sha256": "1", "assembly_sha256": "2", "inventory_sha256": "3"},
            "extractor": {"binary_sha256": "4", "inventory_sha256": "5"},
            "libraries": [{"name": "P.msp", "sha256": "6"}, {"name": "L.lbm2", "sha256": "7"}],
            "interactive": {"version": "0.5.16", "commit": "c1", "dirty": False},
            "catalog": {"version": "0.6.1", "commit": "c2", "dirty": False},
            "gate": {"commit": "c3", "dirty": False},
        }
        import copy

        self.assertEqual(policy.pin_differences(recorded, copy.deepcopy(recorded)), [])
        moved = copy.deepcopy(recorded)
        moved["console"]["path"] = "D:/elsewhere/MSDIALCUI.exe"
        self.assertEqual(policy.pin_differences(recorded, moved), [], "a path is not an identity")
        changed = copy.deepcopy(recorded)
        changed["console"]["inventory_sha256"] = "x"
        changed["libraries"][0]["sha256"] = "y"
        changed["libraries"].pop()
        changed["interactive"]["commit"] = "c9"
        changed["gate"]["dirty"] = True
        self.assertEqual(
            policy.pin_differences(recorded, changed),
            ["console.inventory_sha256", "interactive.commit", "gate.dirty", "libraries.L.lbm2.missing", "libraries.P.msp.sha256"],
        )
        stale = copy.deepcopy(recorded)
        stale["extractor"].update(provenance_status="verified", pinned=True)
        later = copy.deepcopy(stale)
        later["extractor"].update(provenance_status="stale_mismatch", pinned=False)
        self.assertEqual(policy.pin_differences(stale, later), ["extractor.provenance_status", "extractor.pinned"])

    def test_a_checkout_the_commit_does_not_identify_is_not_approvable(self) -> None:
        clean = {"interactive": {"commit": "c1", "dirty": False}, "catalog": {"commit": "c2", "dirty": False},
                 "gate": {"commit": "c3", "dirty": False}}
        self.assertEqual(policy.pin_problems(clean), [])
        self.assertEqual(policy.pin_problems({**clean, "gate": {"commit": "c3", "dirty": True}}),
                         ["the gate checkout has uncommitted changes; commit them and plan again"])
        self.assertIn("could not be read (fatal: not a git repository)", policy.pin_problems(
            {**clean, "catalog": {"commit": "", "dirty": None, "error": "fatal: not a git repository"}})[0])


class PrivacyTests(unittest.TestCase):
    def test_a_library_location_is_withheld_in_every_spelling(self) -> None:
        location = r"E:\lab libs\private\set-A\Positive.msp"
        redact = policy.redactor({"Positive.msp": location})
        value = {
            "answers": {"msp_paths": [location, location.replace("\\", "/"), location.lower()]},
            "log": f"loading {location.replace(chr(92), chr(92) * 2)} now",
            "other": r"E:\lab libs\private\set-A\notes.txt",
            location: "as a key",
        }
        redacted = policy.redactor({"Positive.msp": location})(value)
        text = str(redacted)
        self.assertNotIn("lab libs", text.casefold())
        self.assertIn("<library:Positive.msp>", text)
        self.assertIn("<library-directory>", text)
        self.assertEqual(redact("D:/analysis/unit"), "D:/analysis/unit")


if __name__ == "__main__":
    unittest.main()
