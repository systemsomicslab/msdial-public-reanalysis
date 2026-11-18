"""scripts/verify-decision-log.py: the review logs and the decision ledger, walked and refused where they break.

docs/llm-assisted-curation.md section 2.4. Each defect the section lists is made once on an otherwise sound synthetic
campaign (one public-looking accession id, synthetic payloads, synthetic people), and the walker must refuse it with
its own check code while the sound campaign passes. Nothing here runs a model, the Catalog or MS-DIAL.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT / "scripts"))
from campaign import curated  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("verify_decision_log_under_test", ROOT / "scripts" / "verify-decision-log.py")
assert _SPEC and _SPEC.loader
vdl = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_decision_log_under_test"] = vdl
_SPEC.loader.exec_module(vdl)

ACCESSION = "ST000001"
UNIT = "AN000001"
OTHER = "AN000002"
AT = "2026-11-18T00:00:00+00:00"
PERSON = "Synthetic Reviewer"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def payload(unit: str, flags: list[str], note: str = "") -> dict:
    return {"schema": "msdial-unit-triage.v2", "accession": ACCESSION, "unit_id": unit, "status": "needs_person" if flags else "ready",
            "flags": [{"code": code, "certainty": "suspected", "detail": "synthetic"} for code in flags],
            "rows": [], "notes": note}


class Campaign:
    """<root>/campaign with triage/ST000001/: AN000001.json (flagged mixed_acquisition), its revision AN000001.r1.json (the
    flag dropped), AN000002.json; and the review log and decision ledger a sound campaign would hold."""

    def __init__(self, root: Path) -> None:
        self.directory = root / "campaign"
        self.triage = self.directory / "triage"
        self.folder = self.triage / ACCESSION
        self.folder.mkdir(parents=True)
        (self.directory / "campaign-manifest.json").write_text(
            json.dumps({"units": [], "triage": {"directory": str(self.triage)}}), encoding="utf-8")
        self.base = self.write(f"{UNIT}.json", payload(UNIT, ["mixed_acquisition"]))
        self.revision = self.write(f"{UNIT}.r1.json", payload(UNIT, [], "revised on the reviewer's answer"))
        self.other = self.write(f"{OTHER}.json", payload(OTHER, []))
        self.events: list[dict] = []

    def write(self, name: str, value: dict) -> str:
        data = json.dumps(value, indent=1).encode("utf-8")
        (self.folder / name).write_bytes(data)
        return sha(data)

    def log(self, kind: str, decision_id: str | None, **fields) -> dict:
        return curated.append_review_log(self.folder, kind, decision_id, AT, **fields)

    def verdict(self, decision_id: str, verdict: str = "hold") -> dict:
        return self.log("validator_verdict", decision_id, tool="validate-triage.py", code_commit=None, verdict=verdict,
                        findings_sha256="0" * 64, findings=[])

    def answer(self, decision_id: str, code: str, by: str = PERSON) -> dict:
        return self.log("person_answer", decision_id, by=by, question_code=code, answer="One run.", channel="conversation")

    def revise(self, cause: dict, *, supersedes: str | None = None, new: str | None = None) -> dict:
        return self.log("revision", supersedes or self.base, supersedes=supersedes or self.base,
                        new_decision_id=new or self.revision, cause=cause)

    def event(self, kind: str, unit_key: str | None, **detail) -> None:
        self.events.append({"seq": len(self.events) + 1, "at": AT, "kind": kind, "unit_key": unit_key,
                            "detail_json": json.dumps(detail)})

    def export(self) -> None:
        target = self.directory / curated.DECISION_LEDGER
        target.unlink(missing_ok=True)
        curated.export_decision_ledger(target, self.events)

    def sound(self) -> None:
        """The sound history: a hold, a person's answer, the revision on it, the plan's ledger, the approval."""
        self.verdict(self.base)
        answer = self.answer(self.base, "mixed_acquisition")
        self.revise({"kind": "person_answer", "seq": answer["seq"]})
        self.verdict(self.revision, "pass")
        head = curated.review_log_head(self.folder)
        for unit, decision in ((UNIT, self.revision), (OTHER, self.other)):
            name = f"{UNIT}.r1.json" if unit == UNIT else f"{OTHER}.json"
            self.event("triage_recorded", unit, standing="model_inference", accession=ACCESSION, unit_id=unit,
                       triage_relative_path=f"{ACCESSION}/{name}", triage_sha256=decision)
            self.event("triage_validated", unit, standing="deterministic_check", accession=ACCESSION, verdict="pass",
                       review_log_head_sha256=head)
        self.event("manifest_approved", None, standing="accepted_decision", manifest_digest="sha256:" + "1" * 64,
                   approval_id="A1", approved_by=PERSON)
        self.log("digest_approval", self.revision, manifest_digest="sha256:" + "1" * 64, approval_id="A1", by=PERSON)
        self.event("header_reconciliation", UNIT, standing="deterministic_check", verdict="agree")
        self.export()


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.campaign = Campaign(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def verify(self, directory: Path | None = None, **options):
        return vdl.verify(directory or self.campaign.directory, **options)

    @staticmethod
    def checks(report, level: str = "error") -> list[str]:
        return [item["check"] for item in report.findings if item["level"] == level]

    def refused_by(self, check: str, report=None) -> list[str]:
        report = report or self.verify()
        self.assertEqual(report.verdict, "refused", json.dumps(report.as_dict(), indent=1))
        found = [item["message"] for item in report.findings if item["check"] == check and item["level"] == "error"]
        self.assertTrue(found, json.dumps(report.as_dict(), indent=1))
        return found

    def rewrite_line(self, path: Path, number: int, change) -> None:
        lines = path.read_bytes().split(b"\n")
        value = json.loads(lines[number - 1])
        change(value)
        lines[number - 1] = curated.canonical(value)
        path.write_bytes(b"\n".join(lines))


class SoundCampaignTests(_Case):
    def test_a_sound_campaign_passes(self) -> None:
        self.campaign.sound()
        report = self.verify()
        self.assertEqual(report.verdict, "pass", json.dumps(report.as_dict(), indent=1))
        self.assertEqual(report.findings, [])
        self.assertEqual(report.accessions[ACCESSION]["payloads"], 3)
        self.assertEqual(report.ledger["triage_directory"], "triage")

    def test_the_command_line_exits_0_and_1(self) -> None:
        self.campaign.sound()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(vdl.main([str(self.campaign.directory), "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["verdict"], "pass")
        self.campaign.answer("f" * 64, "ms1_only")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(vdl.main([str(self.campaign.directory)]), 1)
            self.assertEqual(vdl.main([str(self.root / "missing")]), 2)

    def test_without_a_ledger_the_review_logs_are_checked_and_it_is_said(self) -> None:
        self.campaign.verdict(self.campaign.base)
        report = self.verify()
        self.assertEqual(report.verdict, "pass")
        self.assertEqual(self.checks(report, "note"), ["D-CHAIN"])

    def test_the_triage_directory_is_found_beside_the_campaign_or_named(self) -> None:
        self.campaign.sound()
        (self.campaign.directory / "campaign-manifest.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.verify().verdict, "pass")
        moved = self.root / "elsewhere"
        self.campaign.triage.rename(moved)
        self.refused_by("D-TRIAGE")
        self.assertEqual(self.verify(triage_dir=moved).verdict, "pass")


class ReviewLogTests(_Case):
    def test_a_broken_link_is_refused(self) -> None:
        self.campaign.sound()
        log = self.campaign.folder / curated.REVIEW_LOG
        self.rewrite_line(log, 2, lambda value: value.update(answer="Out of scope."))
        found = self.refused_by("L-CHAIN")
        self.assertTrue(any("line 3 does not chain to line 2" in message for message in found), found)

    def test_a_line_that_does_not_parse_or_counts_wrong_is_refused(self) -> None:
        self.campaign.sound()
        log = self.campaign.folder / curated.REVIEW_LOG
        log.write_bytes(log.read_bytes() + b"not json\n")
        self.refused_by("L-CHAIN")

    def test_a_decision_id_without_its_payload_is_refused(self) -> None:
        self.campaign.sound()
        self.campaign.answer("f" * 64, "ms1_only")
        found = self.refused_by("L-PAYLOAD")
        self.assertTrue(any("f" * 64 in message for message in found))

    def test_a_revision_naming_an_unknown_decision_is_refused(self) -> None:
        answer = self.campaign.answer(self.campaign.base, "mixed_acquisition")
        self.campaign.revise({"kind": "person_answer", "seq": answer["seq"]}, new="e" * 64)
        found = self.refused_by("L-PAYLOAD")
        self.assertTrue(any("new_decision_id" in message for message in found), found)

    def test_an_unknown_kind_or_field_is_refused(self) -> None:
        self.campaign.sound()
        log = self.campaign.folder / curated.REVIEW_LOG
        # Appended past the writer, which refuses both: what a hand edit would leave.
        lines = [line for line in log.read_bytes().split(b"\n") if line.strip()]
        entry = {"seq": len(lines) + 1, "at": AT, "kind": "header_verdict", "decision_id": self.campaign.base,
                 "prev_sha256": sha(lines[-1])}
        log.write_bytes(log.read_bytes() + curated.canonical(entry) + b"\n")
        self.refused_by("L-ENTRY")


class RevisionCauseTests(_Case):
    def test_a_cause_naming_no_entry_of_its_kind_is_refused(self) -> None:
        verdict = self.campaign.verdict(self.campaign.base)
        self.campaign.answer(self.campaign.base, "mixed_acquisition")
        self.campaign.revise({"kind": "person_answer", "seq": verdict["seq"]})
        found = self.refused_by("L-CAUSE")
        self.assertTrue(any("holds no person_answer there" in message for message in found), found)

    def test_a_cause_at_a_seq_beyond_the_log_or_of_an_unknown_kind_is_refused(self) -> None:
        self.campaign.revise({"kind": "person_answer", "seq": 99})
        self.refused_by("L-CAUSE")
        campaign = Campaign(self.root / "second")
        campaign.revise({"kind": "operator_request", "seq": 1})
        self.refused_by("L-CAUSE", vdl.verify(campaign.directory))

    def test_a_cause_after_the_revision_or_about_another_decision_is_refused(self) -> None:
        answer = self.campaign.answer(self.campaign.other, "mixed_acquisition")
        self.campaign.revise({"kind": "person_answer", "seq": answer["seq"]})
        found = self.refused_by("L-CAUSE")
        self.assertTrue(any("does not supersede" in message for message in found), found)
        campaign = Campaign(self.root / "second")
        campaign.revise({"kind": "person_answer", "seq": 2})
        campaign.answer(campaign.base, "mixed_acquisition")
        found = self.refused_by("L-CAUSE", vdl.verify(campaign.directory))
        self.assertTrue(any("comes after it" in message for message in found), found)

    def test_a_validator_verdict_is_an_admissible_cause(self) -> None:
        campaign = self.campaign
        # A revision that drops no blocking flag, caused by a verdict about the payload it supersedes.
        same = campaign.write(f"{OTHER}.r1.json", payload(OTHER, [], "reworded"))
        verdict = campaign.verdict(campaign.other)
        campaign.revise({"kind": "validator_verdict", "seq": verdict["seq"]}, supersedes=campaign.other, new=same)
        report = self.verify()
        self.assertEqual(self.checks(report), [], json.dumps(report.as_dict(), indent=1))

    def test_a_ledger_header_reconciliation_cause_must_be_in_the_ledger_and_about_the_unit(self) -> None:
        campaign = self.campaign
        same = campaign.write(f"{OTHER}.r1.json", payload(OTHER, [], "reworded"))
        campaign.event("header_reconciliation", f"{OTHER}-pos", standing="deterministic_check", verdict="disagree")
        campaign.event("header_reconciliation", UNIT, standing="deterministic_check", verdict="disagree")
        campaign.revise({"kind": "header_reconciliation", "seq": 1}, supersedes=campaign.other, new=same)
        found = self.refused_by("L-CAUSE")
        self.assertTrue(any("no decision ledger" in message for message in found), found)
        campaign.export()
        self.assertEqual(self.checks(self.verify()), [])
        campaign.revise({"kind": "header_reconciliation", "seq": 2}, supersedes=campaign.other, new=same)
        found = self.refused_by("L-CAUSE")
        self.assertTrue(any(f"about {UNIT}, not {OTHER}" in message for message in found), found)


class DroppedFlagTests(_Case):
    def test_a_revision_dropping_a_blocking_flag_needs_an_answer_to_that_code(self) -> None:
        verdict = self.campaign.verdict(self.campaign.base)
        self.campaign.revise({"kind": "validator_verdict", "seq": verdict["seq"]})
        found = self.refused_by("L-FLAG")
        self.assertTrue(any("mixed_acquisition" in message for message in found), found)
        other = self.campaign.answer(self.campaign.base, "ms1_only")
        self.campaign.revise({"kind": "person_answer", "seq": other["seq"]})
        self.refused_by("L-FLAG")
        answer = self.campaign.answer(self.campaign.base, "mixed_acquisition")
        self.campaign.revise({"kind": "person_answer", "seq": answer["seq"]})
        report = self.verify()
        self.assertNotIn("L-FLAG", self.checks(report), json.dumps(report.as_dict(), indent=1))
        self.assertEqual(report.verdict, "pass", json.dumps(report.as_dict(), indent=1))

    def test_an_unnamed_answer_does_not_clear_a_flag(self) -> None:
        log = self.campaign.folder / curated.REVIEW_LOG
        answer = self.campaign.answer(self.campaign.base, "mixed_acquisition")
        self.rewrite_line(log, answer["seq"], lambda value: value.update(by=" "))
        self.campaign.revise({"kind": "person_answer", "seq": answer["seq"]})
        self.refused_by("L-FLAG")

    def test_a_flag_that_is_not_blocking_may_go(self) -> None:
        campaign = self.campaign
        noted = campaign.write(f"{OTHER}.r1.json", payload(OTHER, []))
        (campaign.folder / f"{OTHER}.json").unlink()
        flagged = campaign.write(f"{OTHER}.json", payload(OTHER, ["low_listing_coverage"]))
        verdict = campaign.verdict(flagged)
        campaign.revise({"kind": "validator_verdict", "seq": verdict["seq"]}, supersedes=flagged, new=noted)
        self.assertEqual(self.checks(self.verify()), [])


class LedgerTests(_Case):
    def test_a_broken_ledger_link_is_refused(self) -> None:
        self.campaign.sound()
        self.rewrite_line(self.campaign.directory / curated.DECISION_LEDGER, 2,
                          lambda value: value["detail"].update(verdict="hold"))
        self.refused_by("D-CHAIN")

    def test_a_triage_recorded_naming_no_payload_is_refused(self) -> None:
        self.campaign.sound()
        self.campaign.event("triage_recorded", "AN000003", standing="model_inference", accession=ACCESSION,
                            triage_sha256="d" * 64)
        self.campaign.export()
        found = self.refused_by("D-TRIAGE")
        self.assertTrue(any("no payload's sha256" in message for message in found), found)

    def test_a_triage_recorded_whose_named_file_changed_is_refused(self) -> None:
        self.campaign.sound()
        path = self.campaign.folder / f"{OTHER}.json"
        path.write_bytes(path.read_bytes() + b"\n")
        found = self.refused_by("D-TRIAGE")
        self.assertTrue(any(f"triage_recorded, {OTHER}" in message for message in found), found)

    def test_a_triage_validated_head_that_is_no_line_of_the_log_is_refused(self) -> None:
        self.campaign.sound()
        self.campaign.event("triage_recorded", OTHER, standing="model_inference", accession=ACCESSION,
                            triage_relative_path=f"{ACCESSION}/{OTHER}.json", triage_sha256=self.campaign.other)
        self.campaign.event("triage_validated", OTHER, standing="deterministic_check", accession=ACCESSION,
                            review_log_head_sha256="c" * 64)
        self.campaign.export()
        found = self.refused_by("D-HEAD")
        self.assertTrue(any("c" * 64 in message for message in found), found)

    def test_a_null_head_passes(self) -> None:
        self.campaign.event("triage_recorded", OTHER, standing="model_inference", accession=ACCESSION,
                            triage_relative_path=f"{ACCESSION}/{OTHER}.json", triage_sha256=self.campaign.other)
        self.campaign.event("triage_validated", OTHER, standing="deterministic_check", accession=ACCESSION,
                            review_log_head_sha256=None)
        self.campaign.export()
        self.assertEqual(self.verify().verdict, "pass")

    def test_an_absolute_path_or_an_unknown_kind_in_the_ledger_is_refused(self) -> None:
        self.campaign.sound()
        ledger = self.campaign.directory / curated.DECISION_LEDGER
        lines = [line for line in ledger.read_bytes().split(b"\n") if line.strip()]
        for kind, detail, check in (("terminal_refusal", {"note": "see D:\\analysis\\x.log"}, "D-PATH"),
                                    ("operator_request", {}, "D-ENTRY")):
            entry = {"seq": len(lines) + 1, "event_seq": 99, "at": AT, "kind": kind, "unit_key": UNIT,
                     "standing": "deterministic_check", "detail": detail, "prev_sha256": sha(lines[-1])}
            data = curated.canonical(entry)
            ledger.write_bytes(b"".join(line + b"\n" for line in lines) + data + b"\n")
            self.refused_by(check)
        ledger.write_bytes(b"".join(line + b"\n" for line in lines))
        self.assertEqual(self.verify().verdict, "pass")


class ReleaseBundleTests(_Case):
    def test_a_release_bundle_is_walked_as_released(self) -> None:
        self.campaign.sound()
        bundle = self.root / "bundle"
        curated.write_release(campaign_dir=self.campaign.directory, out=bundle, workspace_root=self.root / "analysis",
                              approved_digest="sha256:" + "1" * 64)
        self.campaign.triage.rename(self.root / "moved")  # the bundle alone
        report = self.verify(bundle)
        self.assertEqual(report.verdict, "pass", json.dumps(report.as_dict(), indent=1))
        self.assertEqual(report.ledger["triage_directory"], "triage")
        self.assertEqual(report.accessions[ACCESSION]["payloads"], 3)


class DocumentTests(unittest.TestCase):
    def test_section_2_4_names_every_check_the_walker_reports(self) -> None:
        text = (ROOT / "docs" / "llm-assisted-curation.md").read_text(encoding="utf-8")
        section = text[text.index("### 2.4"):text.index("### 2.5")]
        source = (ROOT / "scripts" / "verify-decision-log.py").read_text(encoding="utf-8")
        import re  # noqa: PLC0415

        codes = sorted(set(re.findall(r'"([LD]-[A-Z]+)"', source)))
        self.assertEqual(codes, ["D-CHAIN", "D-ENTRY", "D-HEAD", "D-PATH", "D-TRIAGE",
                                 "L-CAUSE", "L-CHAIN", "L-ENTRY", "L-FLAG", "L-PAYLOAD"])
        for code in codes:
            self.assertIn(f"`{code}`", section, code)


if __name__ == "__main__":
    unittest.main()
