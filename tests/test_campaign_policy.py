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
            "ELIG-1", "ACQ-1", "SUM-1", "CNT-1", "INP-1", "CLS-1", "CLS-2", "CLS-3", "ORD-1", "PKH-1")))
        self.assertEqual(policy.run_blocking_failures(report), (sorted(policy.BLOCKS_RUN_CHECKS), "runner_default"))

    def test_only_a_fail_blocks(self) -> None:
        report = self.report({"check_id": "SUM-1", "status": "warn"}, {"check_id": "INP-1", "status": "not_evaluable"},
                             {"check_id": "ELIG-1", "status": "pass"}, {"check_id": "ACQ-1", "status": "FAIL"})
        self.assertEqual(policy.run_blocking_failures(report), (["ACQ-1"], "runner_default"))

    def test_the_gates_run_policy_decides_when_it_states_one(self) -> None:
        per_check = self.report({"check_id": "SUM-1", "status": "fail", "run_policy": "record_only"},
                                {"check_id": "CLS-1", "status": "fail", "run_policy": "blocks_run"},
                                {"check_id": "ORD-1", "status": "fail"})
        self.assertEqual(policy.run_blocking_failures(per_check), (["CLS-1"], "gate"))
        by_list = self.report({"check_id": "SUM-1", "status": "fail"}, {"check_id": "PKH-1", "status": "fail"},
                              run_policy={"blocks_run": ["PKH-1"], "record_only": ["SUM-1"]})
        self.assertEqual(policy.run_blocking_failures(by_list), (["PKH-1"], "gate"))
        by_check = self.report({"check_id": "INP-1", "status": "fail"}, run_policy={"INP-1": "record_only"})
        self.assertEqual(policy.run_blocking_failures(by_check), ([], "gate"))


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
