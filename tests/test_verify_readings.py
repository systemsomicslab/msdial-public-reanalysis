"""READ-1 and scripts/record-reading.py: a sentence left for a person holds the run until it is read.

The user decided on 2026-09-28 that SUM-2's matching sentences set aside by a rule and QA-1's
sentences that are not Interactive's own hold a run from counting as completed until a person's
reading of them is recorded, against a digest of the sentences, so that a changed text is read
again.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_qa_prose as prose  # noqa: E402  (loads the gate as verify_run_invariants)
import test_verify_split_and_progress as split  # noqa: E402

verifier = prose.verifier
_SPEC = importlib.util.spec_from_file_location("record_reading", TESTS.parent / "scripts" / "record-reading.py")
assert _SPEC and _SPEC.loader
recorder = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(recorder)

ADDED = "QC precision was excellent."


def _read1(root: Path):
    report = verifier.verify(root, "before-publish")
    return next(check for check in report.checks if check.check_id == "READ-1"), report


def _record(root: Path, *arguments: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = recorder.main([str(root), *arguments])
    return code, out.getvalue(), err.getvalue()


def _digest(root: Path, check_id: str = "QA-1") -> str:
    report = verifier.verify(root, "before-publish")
    return next(check for check in report.checks if check.check_id == check_id).evidence["to_read_digest"]


class ReadingTests(unittest.TestCase):
    def workspace(self, directory: str, results: str | None = None) -> Path:
        fixture = prose.INTERACTIVE_052
        results = results if results is not None else prose._append(fixture, ADDED)
        return prose._real(Path(directory), fixture, results=results).root

    def test_nothing_to_read_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            check, _ = _read1(self.workspace(directory, prose.INTERACTIVE_052["results"]))

        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_a_sentence_to_read_holds_the_run_until_its_reading_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory)
            before, _ = _read1(root)
            code, shown, _ = _record(root, "--check", "QA-1")
            nothing = not (root / "provenance" / "readings.json").exists()
            digest = _digest(root)
            code_recorded, _, _ = _record(root, "--check", "QA-1", "--digest", digest, "--by", "Hiroshi Tsugawa",
                                          "--conclusion", "accepted", "--note", "a true remark")
            after, _ = _read1(root)
            saved = json.loads((root / "provenance" / "readings.json").read_text(encoding="utf-8"))

        self.assertEqual(verifier.NOT_EVALUABLE, before.status, before.detail)
        self.assertTrue(before.required)
        self.assertEqual(0, code)
        self.assertIn(ADDED, shown)
        self.assertIn(digest, shown)
        self.assertTrue(nothing, "showing the sentences must record nothing")
        self.assertEqual(0, code_recorded)
        self.assertEqual(verifier.PASS, after.status, after.detail)
        self.assertIn("read by Hiroshi Tsugawa", after.detail)
        entry = saved["readings"][0]
        self.assertEqual(("QA-1", digest, "accepted", 1), (entry["check_id"], entry["digest"], entry["conclusion"],
                                                          entry["sentence_count"]))
        self.assertEqual(verifier.READINGS_SCHEMA, saved["schema"])

    def test_a_changed_text_is_read_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory)
            _record(root, "--check", "QA-1", "--digest", _digest(root), "--by", "A", "--conclusion", "accepted")
            results = root / "output" / "MS_DIAL_QA_Results.txt"
            results.write_text(results.read_text(encoding="utf-8").rstrip() + " Carryover was negligible.\n",
                               encoding="utf-8")
            check, _ = _read1(root)

        self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)
        self.assertTrue(check.evidence["stale"])

    def test_the_same_sentences_rewrapped_keep_their_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory)
            _record(root, "--check", "QA-1", "--digest", _digest(root), "--by", "A", "--conclusion", "accepted")
            results = root / "output" / "MS_DIAL_QA_Results.txt"
            results.write_text(results.read_text(encoding="utf-8").replace("QC precision was", "QC precision\r\nwas"),
                               encoding="utf-8")
            check, _ = _read1(root)

        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_a_reading_that_finds_the_text_wrong_is_refused_until_it_is_corrected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory)
            digest = _digest(root)
            _record(root, "--check", "QA-1", "--digest", digest, "--by", "A", "--conclusion", "accepted")
            _record(root, "--check", "QA-1", "--digest", digest, "--by", "B", "--conclusion", "rejected",
                    "--note", "there were no QC injections")
            wrong, _ = _read1(root)
            results = root / "output" / "MS_DIAL_QA_Results.txt"
            results.write_text(prose.INTERACTIVE_052["results"], encoding="utf-8")
            corrected, _ = _read1(root)

        self.assertEqual(verifier.FAIL, wrong.status, wrong.detail)
        self.assertIn("there were no QC injections", wrong.detail)
        self.assertEqual(verifier.PASS, corrected.status, corrected.detail)

    def test_the_recorder_refuses_what_it_did_not_show(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory)
            wrong = _record(root, "--check", "QA-1", "--digest", "sha256:" + "0" * 64, "--by", "A",
                            "--conclusion", "accepted")
            nobody = _record(root, "--check", "QA-1", "--digest", _digest(root), "--by", " ", "--conclusion", "accepted")
            no_conclusion = _record(root, "--check", "QA-1", "--digest", _digest(root), "--by", "A")
            recorded = (root / "provenance" / "readings.json").exists()

        self.assertEqual(2, wrong[0])
        self.assertIn("the text changed since it was shown", wrong[2])
        self.assertEqual(2, nobody[0])
        self.assertEqual(2, no_conclusion[0])
        self.assertFalse(recorded)

    def test_a_readings_file_that_cannot_be_read_is_neither_trusted_nor_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory)
            path = root / "provenance" / "readings.json"
            path.write_text('{"readings": "yes"}', encoding="utf-8")
            check, _ = _read1(root)
            code, _, err = _record(root, "--check", "QA-1", "--digest", _digest(root), "--by", "A",
                                   "--conclusion", "accepted")
            kept = path.read_text(encoding="utf-8")

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertIn("is not a", " ".join(check.evidence["problems"]))
        self.assertEqual(2, code)
        self.assertEqual('{"readings": "yes"}', kept)

    def test_qa1_that_could_not_read_the_texts_leaves_read1_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory)
            for name in ("MS_DIAL_Materials_and_Methods.txt", "MS_DIAL_QA_Results.txt"):
                (root / "output" / name).unlink()
            check, _ = _read1(root)

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)

    def test_sum2_sentences_set_aside_are_read_too(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = split.SplitFixture(Path(temporary))
            root = fixture.part_roots[0]
            (root / "output" / "MS_DIAL_Materials_and_Methods.txt").write_text(
                "Raw files were not checksum-verified, because the repository publishes no checksums.",
                encoding="utf-8")
            report = fixture.gate(0, "before-publish")
            sum2 = next(check for check in report.checks if check.check_id == "SUM-2")
            before = next(check for check in report.checks if check.check_id == "READ-1")
            code, shown, _ = _record(root, "--check", "SUM-2")
            digest = sum2.evidence["to_read_digest"]
            _record(root, "--check", "SUM-2", "--digest", digest, "--by", "A", "--conclusion", "accepted")
            after = next(check for check in fixture.gate(0, "before-publish").checks if check.check_id == "READ-1")

        self.assertEqual(verifier.WARN, sum2.status, sum2.detail)
        self.assertEqual(1, sum2.evidence["to_read_count"])
        self.assertEqual(verifier.NOT_EVALUABLE, before.status, before.detail)
        self.assertIn("SUM-2", before.evidence["pending"])
        self.assertIn("not checksum-verified", shown)
        # The fixture has no publication report, so QA-1 could not be evaluated: READ-1 does not PASS,
        # and says that SUM-2's sentences were read.
        self.assertEqual(verifier.NOT_EVALUABLE, after.status, after.detail)
        self.assertFalse(after.required)
        self.assertIn("SUM-2: 1 sentence(s) read by A", after.detail)

    def test_a_persons_words_inside_interactives_sentences_are_read(self) -> None:
        results = prose.INTERACTIVE_052["results"].replace(
            "review were: Absolute run-order/intensity correlation.",
            "review were: Absolute run-order/intensity correlation, although the drift is explained by maintenance.")
        with tempfile.TemporaryDirectory() as directory:
            check, report = _read1(self.workspace(directory, results))
        qa1 = next(item for item in report.checks if item.check_id == "QA-1")

        self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)
        self.assertIn("explained by maintenance", " ".join(item["sentence"] for item in qa1.evidence["to_read"]))

    def test_a_table_row_qa1_cannot_read_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = prose.INTERACTIVE_052
            root = prose._real(Path(directory), fixture,
                               extra=["Quality assurance\tAbsolute run-order/intensity correlation\tComment\t"
                                      "drift explained by maintenance\t\tx"]).root
            check, report = _read1(root)
        qa1 = next(item for item in report.checks if item.check_id == "QA-1")

        self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)
        self.assertIn("comment | drift explained by maintenance",
                      " ".join(item["sentence"] for item in qa1.evidence["to_read"]))

    def test_a_text_qa1_could_not_read_holds_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory, prose.INTERACTIVE_052["results"])
            (root / "output" / "MS_DIAL_QA_Results.txt").write_bytes(b"Quality \xe9t\xe9 \xff")
            check, _ = _read1(root)

        self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)
        self.assertTrue(check.required)
        self.assertIn("could not be read", check.detail)

    def test_every_sentence_is_shown_and_covered(self) -> None:
        many = " ".join(f"Carryover note {index} was reviewed." for index in range(250))
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory, prose._append(prose.INTERACTIVE_052, many))
            report = verifier.verify(root, "before-publish")
            qa1 = next(item for item in report.checks if item.check_id == "QA-1")

        self.assertEqual(250, qa1.evidence["to_read_count"])
        self.assertEqual(250, len(qa1.evidence["to_read"]))

    def test_readings_the_recorder_would_not_write_are_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory)
            digest = _digest(root)
            for name, entry in {
                "an odd conclusion": {"check_id": "QA-1", "digest": digest, "conclusion": "Accepted", "read_by": "A",
                                      "read_at": "2026-09-29T10:00:00+09:00"},
                "no reader": {"check_id": "QA-1", "digest": digest, "conclusion": "accepted", "read_by": " ",
                              "read_at": "2026-09-29T10:00:00+09:00"},
                "no time": {"check_id": "QA-1", "digest": digest, "conclusion": "accepted", "read_by": "A",
                            "read_at": "yesterday"},
            }.items():
                with self.subTest(name):
                    (root / "provenance" / "readings.json").write_text(json.dumps(
                        {"schema": verifier.READINGS_SCHEMA, "readings": [entry]}), encoding="utf-8")
                    check, _ = _read1(root)
                    self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)
                    self.assertTrue(check.evidence["problems"])
            (root / "provenance" / "readings.json").write_text(
                '{"schema": "%s", "readings": [], "readings": []}' % verifier.READINGS_SCHEMA, encoding="utf-8")
            repeated, _ = _read1(root)

        self.assertIn("repeats a key", " ".join(repeated.evidence["problems"]))

    def test_the_latest_reading_of_the_same_sentences_decides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory)
            digest = _digest(root)
            _record(root, "--check", "QA-1", "--digest", digest, "--by", "A", "--conclusion", "rejected")
            _record(root, "--check", "QA-1", "--digest", digest, "--by", "A", "--conclusion", "accepted",
                    "--note", "on second reading the remark is true")
            check, _ = _read1(root)

        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_the_recorder_refuses_to_write_where_it_cannot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.workspace(directory)
            digest = _digest(root)
            (root / "provenance" / "readings.json").mkdir()
            code, _, err = _record(root, "--check", "QA-1", "--digest", digest, "--by", "A", "--conclusion", "accepted")

        self.assertEqual(2, code)
        self.assertIn("refused", err)

    def test_a_digest_names_the_check_the_sentences_belong_to(self) -> None:
        items = [{"source": "a.txt", "sentence": "x"}]
        self.assertNotEqual(verifier._reading_digest("QA-1", items), verifier._reading_digest("SUM-2", items))
        self.assertEqual(verifier._reading_digest("QA-1", items),
                         verifier._reading_digest("QA-1", [{"source": "a.txt", "sentence": " x\n"}]))


if __name__ == "__main__":
    unittest.main()
