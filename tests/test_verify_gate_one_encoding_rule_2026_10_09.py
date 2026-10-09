"""The gate on the user's one encoding rule of 2026-10-09 ("A: この一つのルールで統一").

When one sample's data arrive in several encodings (S1.raw, S1.mzML, S1.mzXML; copies in other folders included):
1. among the readable ones exactly one is used, the highest in the order vendor format -> mzML -> mzXML;
2. a tie goes to the first by path name (relative, case-insensitive, '/' separators);
3. where the chosen one cannot be read or decoded, or its conversion fails, the next in order is taken;
4. the file used is that sample's own input, paired to its sample row and in its Class;
5. every file not used is recorded with its reason, naming the file used.

Interactive 0.5.36 (msdial-interactive-app#69, msdial_app.encoding_rule) records each choice in
manifest.encoding_choices and on the used input's lineage row (encoding_choice). The gate recomputes the rule from
that record: INP-1 (blocks_run) refuses a run that departs from it or lacks the record, CONV-1 holds the choices to
the conversions, and PAIR-1 (record_only) lists the choices and holds the record itself. The rule supersedes the
case-by-case answers of 2026-10-08 (the readable twin of an undecodable mzML, chosen_other_encoding, a copy nearest
the data root), whose checks and tests are gone.

The fixtures are test_verify_gate_decisions_2026_10_08's ST001264-like unit-scoped archive (no declaration), and
test_verify_lineage_built_csv's FolderBranchUnit for a declared unit.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_gate_decisions_2026_10_08 as d08  # noqa: E402
from test_verify_lineage_built_csv import FolderBranchUnit  # noqa: E402

verifier = d08.verifier
_check = d08._check
LOWER, TIE = "lower_in_encoding_order", "tie_lexicographic"


def _choice(used: "str | None", *unused: "tuple[str, str]") -> dict:
    return {"rule": verifier.ENCODING_RULE, "used": used,
            "unused": [{"path": path, "reason": reason} for path, reason in unused]}


def _problems(choice: dict, *, campaign: bool = False) -> list[str]:
    return verifier._encoding_rule_problems(verifier._read_encoding_choice(choice, "test"), campaign)


def _record(unit, choice: dict, *, listed: bool = True, carried: bool = True, stands_for: str = "") -> None:
    """The lease's record of one sample's choice: in manifest.encoding_choices, and on the lineage row of the input
    used (a converted input's through the mzXML it was converted from)."""
    if listed:
        unit.manifest.setdefault("encoding_choices", []).append(choice)
    if not carried or not choice["used"]:
        return
    root = Path(unit.manifest["input_directory"])
    for row in unit.manifest["input_lineage"]["rows"]:
        source = ((row.get("source") or {}).get("conversion") or {}).get("source_path") or row["path"]
        if Path(os.path.relpath(source, root)).as_posix().casefold() == choice["used"].casefold():
            row["encoding_choice"] = {**choice, **({"stands_for": stands_for} if stands_for else {})}
            return
    raise AssertionError(f"no lineage row is {choice['used']}")


def _lease_exclude(unit, relative: str, reason: str = "unsupported_mzml_encoding") -> str:
    """An encoding the lease excluded itself (an mzML RawDataHandler cannot decode), with no declaration."""
    path = unit.data / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<mzML/>")
    unit.manifest.setdefault("excluded_input_candidates", []).append({"path": str(path), "reason": reason})
    unit.manifest["input_lineage"]["excluded"].append(
        {"path": str(path), "kind": "file", "declared_names": [], "sample_id": "", "exclusion": {"reason": reason}})
    return str(path)


class TheRuleRecomputedTests(unittest.TestCase):
    """_encoding_rule_problems recomputes the rule from a choice's candidates and reasons, clause by clause."""

    def test_the_ranks(self) -> None:
        rank = verifier._encoding_rank
        for path, expected in (("S1.raw", 0), ("S1.d", 0), ("x/S1.wiff2", 0), ("S1.d.zip", 0), ("S1.lcd", 0),
                               ("S1.cdf", 1), ("S1.abf", 1), ("S1.mzML", 2), ("S1.mzML.gz", 2), ("S1.imzML", 2),
                               ("S1.mzXML", 3), ("S1.mzXML.lzma", 3), ("S1.mzData", None), ("S1.wiff.scan", None),
                               ("README.txt", None)):
            self.assertEqual(expected, rank(path), path)
        self.assertEqual("s1", verifier._encoding_stem("POS/S1.d.zip"))
        self.assertEqual(verifier._encoding_stem("RAW/S1.raw"), verifier._encoding_stem("mzml/s1.mzML"))
        self.assertEqual(frozenset({"Negative"}), verifier._path_polarities("NEG/S1.raw"))
        self.assertEqual(frozenset(), verifier._path_polarities("Neg_Ctrl_1.raw"))
        self.assertEqual(frozenset({"Negative"}), verifier._path_polarities("QC_NEG/Neg_Ctrl_1.raw"))

    def test_clause_1_the_highest_readable_is_used(self) -> None:
        self.assertEqual([], _problems(_choice("S1.raw", ("S1.mzML", LOWER), ("S1.mzXML", LOWER))))
        said = _problems(_choice("S1.mzML", ("S1.raw", LOWER), ("S1.mzXML", LOWER)))
        self.assertTrue(any("leaves S1.raw, before it in the rule's order, unused as lower_in_encoding_order" in item
                            for item in said), said)
        # A re-encoding no instrument writes ranks after every vendor format (outside the rule's words).
        self.assertEqual([], _problems(_choice("S1.raw", ("S1.cdf", LOWER))))
        self.assertTrue(_problems(_choice("S1.cdf", ("S1.raw", TIE))))

    def test_clause_2_a_tie_goes_to_the_first_path_without_case(self) -> None:
        self.assertEqual([], _problems(_choice("RAW/S1.raw", ("S1.raw", TIE))))
        self.assertEqual([], _problems(_choice("a/S1.raw", ("B/S1.raw", TIE))))
        self.assertEqual([], _problems(_choice("S1.d", ("S1.raw", TIE))))
        self.assertTrue(_problems(_choice("S1.raw", ("RAW/S1.raw", TIE))))
        said = _problems(_choice("RAW/S1.raw", ("S1.raw", LOWER), ("S1.mzML", TIE)))
        self.assertIn("leaves S1.raw unused as lower_in_encoding_order, where its rank against RAW/S1.raw makes it "
                      "tie_lexicographic", said)
        self.assertIn("leaves S1.mzML unused as tie_lexicographic, where its rank against RAW/S1.raw makes it "
                      "lower_in_encoding_order", said)

    def test_clause_3_the_next_in_order_where_one_cannot_be_read(self) -> None:
        self.assertEqual([], _problems(_choice("S1.raw", ("S1.mzML", LOWER))))
        self.assertEqual([], _problems(_choice("S1.mzML", ("S1.raw", "raw_header_unreadable"), ("S1.mzXML", LOWER))))
        self.assertEqual([], _problems(_choice("S1.mzXML", ("S1.mzML", "undecodable")), campaign=True))
        self.assertEqual([], _problems(_choice("b/S1.mzXML", ("a/S1.mzXML", "conversion_failed")), campaign=True))
        self.assertEqual([], _problems(_choice("S1.d", ("RAW/S1.d", "incomplete_container"))))
        # Where only a header stood in the way, the file after the one used keeps its own reason.
        self.assertEqual([], _problems(_choice("S1.raw", ("S1.mzML", "undecodable"))))
        # None could be read: every candidate says why.
        self.assertEqual([], _problems(_choice(None, ("S1.mzML", "undecodable"), ("S1.mzXML", "requires_conversion"))))
        said = _problems(_choice(None, ("S1.mzML", "undecodable"), ("S1.mzXML", LOWER)))
        self.assertTrue(any("uses no file and leaves S1.mzXML (lower_in_encoding_order) unused" in item for item in said))

    def test_clause_5_every_file_not_used_has_its_reason(self) -> None:
        self.assertIn("gives no reason for leaving S1.mzML unused (clause 5)", _problems(_choice("S1.raw", ("S1.mzML", ""))))
        self.assertIn("leaves S1.mzML unused as 'chosen_other_encoding', which is no reason the rule gives",
                      _problems(_choice("S1.raw", ("S1.mzML", "chosen_other_encoding"))))
        self.assertIn("names the rule 'readable_twin_runs_as_the_sample_2026_10_08', not "
                      "one_encoding_per_sample_2026_10_09",
                      _problems({**_choice("S1.raw", ("S1.mzML", LOWER)),
                                 "rule": "readable_twin_runs_as_the_sample_2026_10_08"}))
        self.assertIn("names s1.RAW more than once", _problems(_choice("S1.raw", ("s1.RAW", TIE))))
        self.assertIn("names S1.mzData, which is no encoding the rule ranks (a vendor format, mzML or mzXML)",
                      _problems(_choice("S1.raw", ("S1.mzData", LOWER))))
        # In a campaign an mzXML is converted, so requires_conversion is no reason there.
        self.assertEqual([], _problems(_choice(None, ("S1.mzXML", "requires_conversion"))))
        self.assertTrue(_problems(_choice(None, ("S1.mzXML", "requires_conversion")), campaign=True))

    def test_against_interactive_where_it_is_importable(self) -> None:
        """Every choice Interactive's encoding_rule makes passes the gate's recomputation, and the same choice with
        another file used does not."""
        root = Path(os.environ.get("MSDIAL_INTERACTIVE_ROOT") or r"D:\0_SourceCode\msdial_interactive_app")
        if (root / "msdial_app").is_dir() and str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            from msdial_app import encoding_rule
        except ImportError:
            self.skipTest("this Interactive has no encoding_rule (0.5.36)")
        self.assertEqual(encoding_rule.RULE, verifier.ENCODING_RULE)
        cases = [
            (["S1.raw", "S1.mzML", "S1.mzXML"], {}),
            (["S1.raw", "S1.mzML", "S1.mzXML"], {"S1.raw": "raw_header_unreadable"}),
            (["S1.mzML", "S1.mzXML"], {"S1.mzML": "undecodable"}),
            (["RAW/S1.raw", "S1.raw", "mzML/S1.mzML"], {}),
            (["S1.raw", "S1.d", "S1.wiff"], {}),
            (["S1.raw", "S1.cdf", "S1.abf"], {}),
            (["b/S1.mzXML", "A/S1.mzXML"], {"A/S1.mzXML": "conversion_failed"}),
            (["S1.mzML", "S1.mzXML"], {"S1.mzML": "undecodable", "S1.mzXML": "requires_conversion"}),
            (["S1.d", "x/S1.d"], {"S1.d": "incomplete_container"}),
        ]
        for paths, unreadable in cases:
            with self.subTest(paths=paths, unreadable=unreadable):
                record = encoding_rule.choose_encoding({path: path for path in paths},
                                                       lambda item: unreadable.get(item, "")).record()
                self.assertEqual([], _problems(record), record)
                if record["used"] and record["unused"]:
                    swapped = {**record, "used": record["unused"][-1]["path"],
                               "unused": [*record["unused"][:-1], {"path": record["used"], "reason": LOWER}]}
                    if unreadable.get(swapped["used"]) is None:
                        self.assertTrue(_problems(swapped), swapped)


class UnattributedUnitTests(unittest.TestCase):
    """A unit-scoped archive with no declaration: INP-1 is held to the rule before the declaration is found
    missing."""

    def test_the_file_used_runs_and_the_others_are_on_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, on_disk=("U_8.mzML", "U_8.mzXML"))
            _record(unit, _choice("U_8.raw", ("U_8.mzML", LOWER), ("U_8.mzXML", LOWER)))
            report = d08._gate(unit)

        self.assertEqual([], report.run_blocked_by)
        inp1 = _check(report, "INP-1")
        self.assertEqual(verifier.NOT_EVALUABLE, inp1.status, inp1.detail)
        self.assertIn("1 sample(s) arrived in more than one encoding, and the one encoding rule of 2026-10-09 "
                      "(one_encoding_per_sample_2026_10_09) used one file of each, recording every other with its "
                      "reason: U_8.raw used (U_8.mzML lower_in_encoding_order, U_8.mzXML lower_in_encoding_order)",
                      inp1.detail)
        self.assertEqual({"lower_in_encoding_order": 2}, inp1.evidence["one_encoding_rule"]["reasons"])
        pair1 = _check(report, "PAIR-1")
        self.assertEqual(verifier.WARN, pair1.status, pair1.detail)
        self.assertIn("U_8.raw used (U_8.mzML lower_in_encoding_order", pair1.detail)
        self.assertEqual(1, pair1.evidence["one_encoding_rule"]["encoding_choices"])

    def test_a_choice_the_rule_does_not_make_blocks_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, plain=("U_8.mzML",), on_disk=("U_8.raw",))
            _record(unit, _choice("U_8.mzML", ("U_8.raw", LOWER)))
            report = d08._gate(unit)

        inp1 = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, inp1.status, inp1.detail)
        self.assertIn("uses U_8.mzML and leaves U_8.raw, before it in the rule's order, unused as "
                      "lower_in_encoding_order", inp1.detail)
        self.assertIn("INP-1", report.run_blocked_by)
        pair1 = _check(report, "PAIR-1")
        self.assertEqual(verifier.FAIL, pair1.status, pair1.detail)
        self.assertEqual(verifier.RECORD_ONLY, pair1.run_policy)

    def test_a_choice_without_its_lineage_record_blocks_the_run(self) -> None:
        cases = {
            "not carried": ({"carried": False}, "manifest.encoding_choices uses U_8.raw for its sample, and its "
                                                "lineage row carries no encoding_choice"),
            "not listed": ({"listed": False}, "U_8.raw's encoding_choice (U_8.raw used (U_8.mzML "
                                              "lower_in_encoding_order)) is not in manifest.encoding_choices"),
        }
        for name, (options, said) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, on_disk=("U_8.mzML",))
                _record(unit, _choice("U_8.raw", ("U_8.mzML", LOWER)), **options)
                report = d08._gate(unit)
                inp1 = _check(report, "INP-1")
                self.assertEqual(verifier.FAIL, inp1.status, inp1.detail)
                self.assertIn(said, inp1.detail)
                self.assertIn("INP-1", report.run_blocked_by)

    def test_a_lineage_choice_of_another_file_blocks_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, on_disk=("U_8.mzML",))
            _record(unit, _choice("U_8.raw", ("U_8.mzML", LOWER)))
            for row in unit.manifest["input_lineage"]["rows"]:
                if row.get("encoding_choice"):
                    row["encoding_choice"]["stands_for"] = str(unit.data / "U_7.mzML")
            report = d08._gate(unit)
            inp1 = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, inp1.status, inp1.detail)
        self.assertIn("stands for", inp1.detail)
        self.assertIn("which is none of its sample's candidates", inp1.detail)

    def test_an_unused_file_that_runs_blocks_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, plain=("U_8.raw", "U_8.mzML"))
            _record(unit, _choice("U_8.raw", ("U_8.mzML", LOWER)))
            report = d08._gate(unit)
            inp1 = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, inp1.status, inp1.detail)
        self.assertIn("1 file(s) the one encoding rule left unused reach the run all the same: U_8.mzML "
                      "(lower_in_encoding_order; U_8.raw used)", inp1.detail)

    def test_two_files_of_one_sample_that_run_without_the_record_block_the_run(self) -> None:
        cases = {
            "vendor and mzML": (("U_8.raw", "U_8.mzML"), (), "U_8.raw and U_8.mzML"),
            "vendor and a converted mzXML": (("U_9.raw",), ("U_9.mzXML",), "U_9.raw and U_9.mzXML"),
            "folders named for their encoding": (("RAW/U_8.raw", "mzML/U_8.mzML"), (),
                                                 "RAW/U_8.raw and mzML/U_8.mzML"),
            "two vendor containers": (("U_8.raw", "U_8.d"), (), "U_8.d and U_8.raw"),
            "copies in two folders": (("A/U_8.raw", "B/U_8.raw"), (), "A/U_8.raw and B/U_8.raw"),
            "a copy of a paired member": ((d08.ST001264[1][: -len(".raw")] + ".mzML",), (),
                                          d08.ST001264[1] + " and " + d08.ST001264[1][: -len(".raw")] + ".mzML"),
        }
        for name, (plain, converted, said) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, plain=plain, converted=converted)
                report = d08._gate(unit)
                check = _check(report, "INP-1")
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn("1 sample(s) reach the run in more than one file, where the one encoding rule uses "
                              "exactly one file of a sample (clauses 1 and 2): " + said, check.detail)
                self.assertIn("INP-1", report.run_blocked_by)

    def test_two_polarities_of_one_stem_are_two_acquisitions(self) -> None:
        """Outside the rule's words: POS/U_8.raw and NEG/U_8.raw are two acquisitions, not two encodings of one."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, plain=("POS/U_8.raw", "NEG/U_8.raw"), converted=())
            report = d08._gate(unit)
            check = _check(report, "INP-1")

        self.assertNotIn("more than one file", check.detail)

    def test_an_input_beside_an_unrecorded_encoding_the_lease_excluded_blocks_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary)
            _lease_exclude(unit, "U_8.mzML")
            report = d08._gate(unit)
            check = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("the lease excluded 1 file(s) of a sample another of whose files runs, and no choice of the one "
                      "encoding rule records them", check.detail)
        self.assertIn("U_8.mzML (unsupported_mzml_encoding) beside U_8.raw", check.detail)

        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary)
            _lease_exclude(unit, "U_7.mzML")
            report = d08._gate(unit)
        self.assertNotIn("no choice of the one encoding rule records them", _check(report, "INP-1").detail)

    def test_clause_4_the_file_used_runs_as_its_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, on_disk=("U_8.mzML",))
            unit.manifest["project"]["sample_metadata"].append({"sample_id": "S8", "raw_file": "U_8.mzML"})
            _record(unit, _choice("U_8.raw", ("U_8.mzML", "undecodable")))
            report = d08._gate(unit)
            check = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("U_8.raw runs as an unattributed member, and it is the file the rule used for a sample a sample "
                      "row names", check.detail)

    def test_clause_3_a_vendor_file_excluded_for_its_header_with_the_next_not_taken(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, on_disk=("U_8.mzML",))
            _record(unit, _choice("U_8.raw", ("U_8.mzML", LOWER)))
            unit.exclude(str(unit.data / "U_8.raw"), "raw_header_unreadable")
            report = d08._gate(unit)
            check = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("U_8.raw, the file the rule used for its sample, is excluded for a raw header the preflight "
                      "could not read (raw_header_unreadable), and the sample's next encoding, U_8.mzML, was not "
                      "taken (encoding_fallback_not_taken)", check.detail)


class ConversionTests(unittest.TestCase):
    """CONV-1 holds each choice to the conversions."""

    def test_a_converted_mzxml_used_for_its_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, on_disk=("U_9.mzML",))
            _record(unit, _choice("U_9.mzXML", ("U_9.mzML", "undecodable")))
            report = d08._gate(unit)

        self.assertEqual([], report.run_blocked_by)
        self.assertEqual(verifier.PASS, _check(report, "CONV-1").status, _check(report, "CONV-1").detail)

    def test_a_conversion_failed_record_against_a_completed_conversion_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, plain=("U_9.raw",))
            unit.manifest["encoding_choices"] = [_choice("U_9.raw", ("U_9.mzXML", "conversion_failed"))]
            report = d08._gate(unit)
            check = _check(report, "CONV-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("U_9.mzXML is left unused as conversion_failed, and its conversion record says it was converted",
                      check.detail)
        self.assertIn("CONV-1", report.run_blocked_by)

    def test_an_mzxml_used_whose_conversion_did_not_complete_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, on_disk=("U_9.mzML",))
            _record(unit, _choice("U_9.mzXML", ("U_9.mzML", "undecodable")))
            unit.manifest["input_conversions"]["records"][0]["status"] = "failed"
            report = d08._gate(unit)
            check = _check(report, "CONV-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("the one encoding rule used U_9.mzXML, an mzXML, for its sample, and its conversion's status is "
                      "'failed', not 'converted'", check.detail)


class DeclaredUnitTests(unittest.TestCase):
    """A declaration listing one sample's file in two folders runs the first by path, and the other is accounted for
    by its sample's choice, not counted missing."""

    def _unit(self, temporary: str) -> FolderBranchUnit:
        unit = FolderBranchUnit(temporary)
        unit.file("RAW/S1.raw", "S1", "A")
        unit.file("S2.mzML", "S2", "B")
        unit._download(unit.data / "S1.raw", b"S1.raw", verified=True)
        project = unit.manifest["project"]
        project["analysis_inputs"].append({"path": "S1.raw", "kind": "file", "sample_id": "S1"})
        project["analysis_input_count"] = len(project["analysis_inputs"])
        return unit

    def test_the_copy_the_rule_left_unused_is_accounted_for(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._unit(temporary)
            _record(unit, _choice("RAW/S1.raw", ("S1.raw", TIE)))
            unit.prepare()
            report = unit.gate()

        inp1 = _check(report, "INP-1")
        self.assertIn(inp1.status, (verifier.PASS, verifier.WARN), inp1.detail)
        self.assertEqual(1, inp1.evidence["counts"]["unused encodings"])
        self.assertIn("1 input(s) the one encoding rule left unused for another file of their sample", inp1.detail)
        self.assertIn("RAW/S1.raw used (S1.raw tie_lexicographic)", inp1.detail)
        self.assertNotIn("INP-1", report.run_blocked_by)

    def test_without_the_record_the_declared_copy_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._unit(temporary)
            unit.prepare()
            report = unit.gate()
            inp1 = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, inp1.status, inp1.detail)
        self.assertIn("the Catalog declared 3 analysis input(s) and the lease found 2 input candidate(s)", inp1.detail)


if __name__ == "__main__":
    unittest.main()
