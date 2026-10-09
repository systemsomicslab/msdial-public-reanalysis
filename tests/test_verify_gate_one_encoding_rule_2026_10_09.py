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

import hashlib
import json
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
UNACCOUNTED = ("of a sample the unit records, and no choice of the one encoding rule, input, exclusion, left_out "
               "record or declaration names them (every file not used is recorded with its reason, and a copy no "
               "record names may be the one the rule uses; clauses 2, 3 and 5): ")


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

    def test_clause_3_a_reason_that_cannot_apply_to_the_encoding_passes_nothing_over(self) -> None:
        """Interactive gives each reason a file could not be read to one encoding: undecodable an mzML's;
        conversion_failed, requires_conversion and polarity_contradicts_declaration an mzXML's; incomplete_container
        and a raw-header reason a vendor file's or a re-encoding's. Any other pairing passes a readable file over."""
        for choice, said in (
            (_choice("S1.mzML", ("S1.raw", "requires_conversion")),
             "leaves S1.raw unused as requires_conversion, a reason only an mzXML is left unused for, and S1.raw is a "
             "vendor format"),
            (_choice("S1.mzML", ("S1.raw", "undecodable")),
             "leaves S1.raw unused as undecodable, a reason only an mzML is left unused for, and S1.raw is a vendor "
             "format"),
            (_choice("S1.mzXML", ("S1.mzML", "incomplete_container")),
             "leaves S1.mzML unused as incomplete_container, a reason only a vendor format or a re-encoding is left "
             "unused for, and S1.mzML is an mzML"),
            (_choice("S1.mzXML", ("S1.mzML", "raw_header_unreadable")),
             "leaves S1.mzML unused as raw_header_unreadable, a reason only a vendor format or a re-encoding is left "
             "unused for, and S1.mzML is an mzML"),
            (_choice("b/S1.raw", ("A/S1.raw", "undecodable")),
             "leaves A/S1.raw unused as undecodable, a reason only an mzML is left unused for"),
            (_choice("S1.raw", ("S1.mzML", "conversion_failed")),
             "leaves S1.mzML unused as conversion_failed, a reason only an mzXML is left unused for"),
        ):
            for campaign in (False, True):
                with self.subTest(choice=choice, campaign=campaign):
                    problems = _problems(choice, campaign=campaign)
                    if campaign and choice["unused"][0]["reason"] == "requires_conversion":
                        self.assertTrue(problems)
                        continue
                    self.assertTrue(any(said in item for item in problems), problems)
        # Each reason on the encoding it belongs to passes, a re-encoding's header included.
        for choice in (_choice("S1.mzML", ("S1.raw", "raw_header_unsupported_format")),
                       _choice("S1.mzML", ("S1.cdf", "raw_header_unreadable")),
                       _choice("S1.mzML", ("S1.d", "incomplete_container")),
                       _choice("S1.mzXML", ("S1.mzML", "undecodable")),
                       _choice("S1.raw", ("S1.mzXML", "polarity_contradicts_declaration")),
                       _choice(None, ("S1.raw", "raw_header_unreadable"), ("S1.mzML", "undecodable"),
                               ("S1.mzXML", "conversion_failed"))):
            with self.subTest(choice=choice):
                self.assertEqual([], _problems(choice, campaign=True))

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


class ReviewRound1Tests(unittest.TestCase):
    """The review of 686e400: a split part read against its raw owner's choices, clause 4 through an inferred
    pairing, a listed copy no record names, one sample's polarities merged as Interactive merges them, and the
    sentence for a sample whose file used was excluded."""

    PREFIXED_RAW = "021518_387057_CSHp_BioRec1.raw"
    PREFIXED_D = "021518_387057_CSHp_BioRec1.d"

    def test_a_split_part_that_runs_a_file_its_owner_left_unused_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "raw" / "data"
            root.mkdir(parents=True)
            raw, mzml = str(root / "U_8.raw"), str(root / "U_8.mzML")
            choice = _choice("U_8.raw", ("U_8.mzML", LOWER))
            owner = {"input_directory": str(root), "input_candidates": [raw], "encoding_choices": [choice],
                     "input_lineage": {"rows": [{"path": raw, "sample_id": "S8", "encoding_choice": choice}],
                                       "excluded": []},
                     "project": {"sample_metadata": [{"sample_id": "S8", "raw_file": "U_8.raw"}]}}
            owner_path = Path(temporary) / "owner.json"
            owner_path.write_text(json.dumps(owner), encoding="utf-8")
            part = {"split_from": {"manifest_path": str(owner_path)}, "input_directory": str(root),
                    "input_candidates": [mzml],
                    "input_lineage": {"rows": [{"path": mzml, "sample_id": "S8"}], "excluded": []}}
            problems, _evidence = verifier._encoding_run_problems(part, None)
            self.assertIn("1 file(s) the one encoding rule left unused reach the run all the same: U_8.mzML "
                          "(lower_in_encoding_order; U_8.raw used) (clause 1: exactly one file of a sample is used)",
                          problems)
            # The part that runs the file used, with its own sample's choice, passes.
            part["input_candidates"] = [raw]
            part["encoding_choices"] = [choice]
            part["input_lineage"]["rows"] = [{"path": raw, "sample_id": "S8"}]
            self.assertEqual([], verifier._encoding_run_problems(part, None)[0])

    def _prefixed_unit(self, temporary: str):
        """ST001264's shape: the row naming BioRec1.raw paired behind a prefix with the .raw, and the .d of that stem
        the rule used (a tie, by path), recorded as Interactive 0.5.36 records it."""
        unit = d08._unit(temporary, plain=(self.PREFIXED_D,), converted=())
        raw_path = str(unit.data / self.PREFIXED_RAW)
        manifest = unit.manifest
        manifest["input_candidates"] = [item for item in manifest["input_candidates"]
                                        if Path(item).name != self.PREFIXED_RAW]
        manifest["input_lineage"]["rows"] = [row for row in manifest["input_lineage"]["rows"]
                                             if Path(row["path"]).name != self.PREFIXED_RAW]
        for row in manifest["input_lineage"]["rows"]:
            if Path(row["path"]).name == self.PREFIXED_D:
                row["name_pairing"] = {"paired_by": "prefixed_member_name", "declared_raw_file": "BioRec1.raw",
                                       "member_name": self.PREFIXED_RAW}
        manifest["unattributed_members"] = {"rule": d08.d07.MEMBER_RULE, "applied": False, "count": 0,
                                            "members": [], "paths": []}
        manifest["warnings"] = ["input_names_paired_by_inference"]
        manifest["inferred_name_pairings"] = [{"member_name": self.PREFIXED_RAW, "declared_raw_file": "BioRec1.raw",
                                               "paired_by": "prefixed_member_name", "encoding_used": self.PREFIXED_D}]
        _record(unit, _choice(self.PREFIXED_D, (self.PREFIXED_RAW, TIE)), stands_for=raw_path)
        return unit

    def test_clause_4_through_an_inferred_pairing(self) -> None:
        said = (f"{self.PREFIXED_D} runs as sample Biorec2, the file the rule used for the sample of Biorec1, whose "
                "rows name its sample's files or were paired with one of them (clause 4)")
        for sample, refused in (("Biorec1", False), ("Biorec2", True)):
            with self.subTest(sample=sample), tempfile.TemporaryDirectory() as temporary:
                unit = self._prefixed_unit(temporary)
                unit.prepare()
                for row in unit.manifest["input_lineage"]["rows"]:
                    if Path(row["path"]).name == self.PREFIXED_D:
                        row["sample_id"] = sample
                problems, _evidence = verifier._encoding_run_problems(unit.manifest, None)
                if refused:
                    self.assertIn(said, problems)
                else:
                    self.assertEqual([], problems)

    def test_a_listed_copy_no_record_names_blocks_the_run(self) -> None:
        said = UNACCOUNTED + "A/U_8.raw beside B/U_8.raw"
        for name, recorded in (("left out of the choice", True), ("no record at all", False)):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, plain=("B/U_8.raw",),
                                 on_disk=("A/U_8.raw", *(("B/U_8.mzML",) if recorded else ())))
                if recorded:
                    _record(unit, _choice("B/U_8.raw", ("B/U_8.mzML", LOWER)))
                report = d08._gate(unit)
                inp1 = _check(report, "INP-1")
                self.assertEqual(verifier.FAIL, inp1.status, inp1.detail)
                self.assertIn(said, inp1.detail)
                self.assertIn("INP-1", report.run_blocked_by)

    def test_a_listed_file_on_record_or_of_another_sample_is_accounted_for(self) -> None:
        def left_out(unit) -> None:
            unit.manifest["unattributed_members"]["left_out"] = [d08._left("B/U_8.raw", "opposite_polarity_name")]
            unit.manifest["unattributed_members"]["left_out_count"] = 1

        cases = {
            "in the choice": (lambda unit: _record(unit, _choice("A/U_8.raw", ("B/U_8.raw", TIE))),
                              ("A/U_8.raw",), ("B/U_8.raw",)),
            "left out on record": (left_out, ("A/U_8.raw",), ("B/U_8.raw",)),
            "another stem": (lambda unit: None, ("U_8.raw",), ("U_80.raw",)),
            "the other polarity": (lambda unit: None, ("U_8.raw",), ("NEG/U_8.raw",)),
        }
        for name, (record, plain, on_disk) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, plain=plain, on_disk=on_disk)
                unit.manifest["project"]["ion_mode"] = "Positive"
                record(unit)
                report = d08._gate(unit)
                inp1 = _check(report, "INP-1")
                self.assertNotIn(UNACCOUNTED, inp1.detail)

    def test_a_path_stating_no_polarity_is_one_sample_with_one_stating_the_units(self) -> None:
        said = ("1 sample(s) reach the run in more than one file, where the one encoding rule uses exactly one file of "
                "a sample (clauses 1 and 2): POS/U_8.raw and U_8.raw")
        for ion_mode, refused in (("Positive", True), ("", True), ("Negative", False)):
            with self.subTest(ion_mode=ion_mode), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, plain=("POS/U_8.raw", "U_8.raw"))
                unit.manifest["project"]["ion_mode"] = ion_mode
                report = d08._gate(unit)
                inp1 = _check(report, "INP-1")
                if refused:
                    self.assertEqual(verifier.FAIL, inp1.status, inp1.detail)
                    self.assertIn(said, inp1.detail)
                else:
                    self.assertNotIn(said, inp1.detail)
        positive = frozenset({"Positive"})
        self.assertEqual({frozenset(): positive, positive: positive},
                         verifier._one_sample_polarities([frozenset(), positive], "Positive"))
        self.assertEqual({}, verifier._one_sample_polarities([positive, frozenset({"Negative"})], ""))

    def test_the_sentence_names_a_sample_whose_file_used_was_excluded(self) -> None:
        for reason in ("raw_header_unreadable", "polarity_mismatch"):
            with self.subTest(reason), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, on_disk=("U_8.mzML",))
                _record(unit, _choice("U_8.raw", ("U_8.mzML", "undecodable")))
                unit.exclude(str(unit.data / "U_8.raw"), reason)
                report = d08._gate(unit)
                inp1 = _check(report, "INP-1")
                self.assertEqual([], report.run_blocked_by)
                self.assertIn("U_8.raw used (U_8.mzML undecodable). 1 of these sample(s) run on no file: the file "
                              "used is excluded or none could be read, and no other file of the sample runs in its "
                              f"place: U_8.raw (excluded, {reason})", inp1.detail)
                self.assertNotIn("the file used runs for each", inp1.detail)


class ReviewRound2Tests(unittest.TestCase):
    """The review of b1f472e: a copy delivered by a download that is no archive is read as an archive-listed one is,
    and a copy no record names is sought for every sample the unit records, not only for one whose input runs: a
    sample whose file used was excluded, or that used none, may have the copy the rule names (clauses 2 and 3)."""

    @staticmethod
    def _download(unit, relative: str) -> None:
        """A file delivered by a plain download (no archive) under the data root, on the raw owner's record."""
        path = unit.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = relative.encode()
        path.write_bytes(data)
        unit.manifest["downloads"].append({"path": str(path), "size_bytes": len(data),
                                           "sha256": hashlib.sha256(data).hexdigest()})

    def _refused(self, report, said: str) -> None:
        inp1 = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, inp1.status, inp1.detail)
        self.assertIn(said, inp1.detail)
        self.assertIn("INP-1", report.run_blocked_by)

    def test_a_downloaded_copy_no_record_names_blocks_the_run(self) -> None:
        for name, recorded in (("left out of the choice", True), ("no record at all", False)):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, plain=("B/U_8.raw",), on_disk=(("B/U_8.mzML",) if recorded else ()))
                self._download(unit, "A/U_8.raw")
                if recorded:
                    _record(unit, _choice("B/U_8.raw", ("B/U_8.mzML", LOWER)))
                self._refused(d08._gate(unit), UNACCOUNTED + "A/U_8.raw beside B/U_8.raw")

    def test_a_downloaded_file_of_another_sample_or_of_no_container_is_accounted_for(self) -> None:
        for name, download in (("another stem", "A/U_80.raw"), ("a companion file of no container", "A/U_8.wiff.scan"),
                               ("outside the data root", "../elsewhere/U_8.raw")):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, plain=("B/U_8.raw",))
                self._download(unit, download)
                inp1 = _check(d08._gate(unit), "INP-1")
                self.assertNotIn(UNACCOUNTED, inp1.detail)

    def _folder_unit(self, temporary: str, *, declared: bool) -> FolderBranchUnit:
        """A MetaboLights-like unit of plain downloads: DERIVED/S1.mzML runs for S1, and RAW/S1.raw, the file the
        rule names (clause 1), was delivered beside it and no record names it."""
        unit = FolderBranchUnit(temporary)
        unit.file("DERIVED/S1.mzML", "S1", "A")
        unit.file("DERIVED/S2.mzML", "S2", "B")
        unit._download(unit.data / "RAW" / "S1.raw", b"RAW/S1.raw", verified=True)
        if not declared:
            project = unit.manifest["project"]
            project["analysis_inputs"] = []
            project["analysis_inputs_declared"] = False
            project.pop("analysis_input_count", None)
            for sample in project["sample_metadata"]:
                sample["raw_file"] = Path(sample["raw_file"]).name
        return unit

    def test_an_undeclared_unit_of_plain_downloads_with_the_vendor_file_unrecorded_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._folder_unit(temporary, declared=False)
            unit.prepare()
            self._refused(unit.gate(), UNACCOUNTED + "RAW/S1.raw beside DERIVED/S1.mzML")

    def test_in_a_declared_unit_the_declaration_keeps_an_undeclared_copy_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._folder_unit(temporary, declared=True)
            unit.prepare()
            inp1 = _check(unit.gate(), "INP-1")
        self.assertNotIn(UNACCOUNTED, inp1.detail)

    def test_a_copy_of_a_sample_whose_file_used_was_excluded_blocks_the_run(self) -> None:
        for reason in ("polarity_mismatch", "raw_header_unreadable"):
            with self.subTest(reason), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, plain=("B/U_8.raw",), on_disk=("A/U_8.raw", "B/U_8.mzML"))
                _record(unit, _choice("B/U_8.raw", ("B/U_8.mzML", LOWER)))
                unit.exclude(str(unit.data / "B/U_8.raw"), reason)
                self._refused(d08._gate(unit), UNACCOUNTED + "A/U_8.raw beside B/U_8.raw")

    def test_a_copy_of_a_sample_that_used_no_file_blocks_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, on_disk=("B/U_7.mzML", "A/U_7.mzML"))
            _lease_exclude(unit, "B/U_7.mzML")
            unit.manifest["encoding_choices"] = [_choice(None, ("B/U_7.mzML", "undecodable"))]
            self._refused(d08._gate(unit), UNACCOUNTED + "A/U_7.mzML beside B/U_7.mzML")
        # With the copy in the choice, the sample's every file is on record.
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, on_disk=("B/U_7.mzML", "A/U_7.mzML"))
            _lease_exclude(unit, "B/U_7.mzML")
            _lease_exclude(unit, "A/U_7.mzML")
            unit.manifest["encoding_choices"] = [_choice(None, ("A/U_7.mzML", "undecodable"),
                                                         ("B/U_7.mzML", "undecodable"))]
            inp1 = _check(d08._gate(unit), "INP-1")
            self.assertNotIn(UNACCOUNTED, inp1.detail)


class ReviewRound3Tests(unittest.TestCase):
    """The review of 909f7da: one sample row's files are one sample's whatever their stems (a member paired by a
    prefix and the file the row names exactly), and in a declared unit an archive member no declaration names stays
    out only on Interactive's left_out record (R2-3), as a plain download stays out by the declaration alone."""

    PREFIXED = "021518_387057_CSHp_BioRec1.raw"

    def _refused(self, report, said: str) -> None:
        inp1 = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, inp1.status, inp1.detail)
        self.assertIn(said, inp1.detail)
        self.assertIn("INP-1", report.run_blocked_by)

    def test_a_copy_of_another_stem_paired_to_the_same_row_no_record_names_blocks_the_run(self) -> None:
        """The row naming BioRec1.raw runs the prefixed member; the archive also holds BioRec1.raw (or a copy in a
        folder), which the row names exactly. Both are that row's sample's (clauses 1 and 2 name the exact copy:
        '000/' sorts before '0215'), and no record names it."""
        for copy in ("BioRec1.raw", "RAW/BioRec1.raw", "000/BioRec1.raw"):
            for recorded in (False, True):
                with self.subTest(copy=copy, recorded=recorded), tempfile.TemporaryDirectory() as temporary:
                    unit = d08._unit(temporary, plain=(), converted=(), on_disk=(copy,))
                    if recorded:
                        # A one-candidate choice that leaves the row's own copy out of the sample.
                        _record(unit, _choice(self.PREFIXED))
                    self._refused(d08._gate(unit), UNACCOUNTED + f"{copy} beside {self.PREFIXED}")

    def _both_run(self, temporary: str):
        """The prefixed member runs for the row naming BioRec1.raw, and 000/BioRec1.raw, which that row names
        exactly, runs as well (an input of its own, sample Biorec1)."""
        unit = d08._unit(temporary, plain=(), converted=())
        download = unit._download(unit.data / "000" / "BioRec1.raw", b"000/BioRec1.raw", verified=True)
        unit._input(download["path"], "file", {"url": download["source_url"], "download_path": download["path"]},
                    {"sha256": download["sha256"], "md5": download["md5"], "declared": download["md5"],
                     "declared_algorithm": "md5", "declared_verified": True})
        return unit

    def test_two_files_of_one_row_of_different_stems_that_both_run_block_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._both_run(temporary)
            rows = unit.prepare()
            self.assertEqual(2, sum(1 for row in rows if row["file_name"] in ("BioRec1", Path(self.PREFIXED).stem)))
            self._refused(unit.gate(), "1 sample(s) reach the run in more than one file, where the one encoding rule "
                                       f"uses exactly one file of a sample (clauses 1 and 2): 000/BioRec1.raw and "
                                       f"{self.PREFIXED}")

    def test_the_rule_followed_across_stems_passes(self) -> None:
        """000/BioRec1.raw runs for the row, and the prefixed member is on record as its tie."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._both_run(temporary)
            manifest = unit.manifest
            manifest["input_candidates"] = [item for item in manifest["input_candidates"]
                                            if Path(item).name != self.PREFIXED]
            manifest["input_lineage"]["rows"] = [row for row in manifest["input_lineage"]["rows"]
                                                 if Path(row["path"]).name != self.PREFIXED]
            manifest["inferred_name_pairings"] = [{"member_name": self.PREFIXED, "declared_raw_file": "BioRec1.raw",
                                                   "paired_by": "prefixed_member_name",
                                                   "encoding_used": "000/BioRec1.raw"}]
            _record(unit, _choice("000/BioRec1.raw", (self.PREFIXED, TIE)))
            unit.prepare()
            for row in manifest["input_lineage"]["rows"]:
                if Path(row["path"]).name == "BioRec1.raw":
                    self.assertEqual("Biorec1", row["sample_id"])
            self.assertEqual([], verifier._encoding_run_problems(manifest, None)[0])

    def test_two_rows_and_two_polarities_stay_two_samples_across_stems(self) -> None:
        """Outside the rule's words, as before: a file each of two rows, and files of two polarities paired to one
        row, are two samples' (_sample_groups)."""
        names = [(0, "biorec1.raw", "Biorec1"), (1, "biorec2.raw", "Biorec2")]
        paired = {self.PREFIXED.casefold(): {"BioRec1.raw"}, "x_biorec2.raw": {"BioRec2.raw"}}
        keys = verifier._sample_groups([self.PREFIXED, "000/BioRec1.raw", "x_BioRec2.raw", "BioRec2.raw"], "",
                                       names, paired)
        self.assertEqual(keys[self.PREFIXED], keys["000/BioRec1.raw"])
        self.assertEqual(keys["x_BioRec2.raw"], keys["BioRec2.raw"])
        self.assertNotEqual(keys[self.PREFIXED], keys["BioRec2.raw"])
        keys = verifier._sample_groups(["POS/X_BioRec1.raw", "NEG/BioRec1.raw", "BioRec1.raw"], "Positive",
                                       names, {"pos/x_biorec1.raw": {"BioRec1.raw"}})
        self.assertEqual(keys["POS/X_BioRec1.raw"], keys["BioRec1.raw"])
        self.assertNotEqual(keys["NEG/BioRec1.raw"], keys["BioRec1.raw"])

    def _declared_archive(self, temporary: str, *, left_out: bool) -> FolderBranchUnit:
        """A declared unit whose unit-scoped archive also extracted RAW/S1.raw, which no declaration names, beside
        the declared S1.mzML; Interactive records it as left out (not_named_by_the_catalog_declaration)."""
        unit = FolderBranchUnit(temporary)
        unit.file("S1.mzML", "S1", "A")
        unit.file("S2.mzML", "S2", "B")
        (unit.data / "RAW").mkdir()
        (unit.data / "RAW" / "S1.raw").write_bytes(b"x")
        d08.earlier._archive(unit, ["S1.mzML", "S2.mzML", "RAW/S1.raw"], shared=1)
        if left_out:
            unit.manifest["unattributed_members"] = {
                "rule": d08.d07.MEMBER_RULE, "applied": False, "count": 0, "members": [], "paths": [],
                "reason": "catalog_declared_inputs", "left_out_count": 1,
                "left_out": [d08._left("RAW/S1.raw", "not_named_by_the_catalog_declaration")]}
        return unit

    def test_in_a_declared_unit_an_undeclared_archive_member_stays_out_only_on_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._declared_archive(temporary, left_out=False)
            unit.prepare()
            self._refused(unit.gate(), UNACCOUNTED + "RAW/S1.raw beside S1.mzML")
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._declared_archive(temporary, left_out=True)
            unit.prepare()
            report = unit.gate()
            inp1 = _check(report, "INP-1")
        self.assertEqual(verifier.PASS, inp1.status, inp1.detail)
        self.assertNotIn("INP-1", report.run_blocked_by)


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


class ReviewRound4Tests(unittest.TestCase):
    """The review of e9e5ca9: a file of a row's own stem in another encoding is that row's sample's, grouped with a
    prefixed or leading-identifier member of the row (Interactive's _encoding_groups, sample_row_of); and a left_out
    entry takes the rule's choice from the file it names only where its reason removes that file under the rule."""

    PREFIXED = "021518_387057_CSHp_BioRec1.raw"
    LEFT_OUT = ("unattributed_members.left_out leaves out 1 file(s) the one encoding rule uses before the file of "
                "their sample that runs, for a reason that does not remove them under the rule")

    def _refused(self, report, said: str) -> None:
        inp1 = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, inp1.status, inp1.detail)
        self.assertIn(said, inp1.detail)
        self.assertIn("INP-1", report.run_blocked_by)

    # ---- (1) a row's files are one sample's whatever their stems ---------------------------------------------

    def test_a_file_of_the_rows_own_stem_is_grouped_with_its_prefixed_member(self) -> None:
        names = [(0, "biorec1.raw", "Biorec1"), (1, "biorec2.raw", "Biorec2")]
        paired = {self.PREFIXED.casefold(): {"BioRec1.raw"}}
        keys = verifier._sample_groups([self.PREFIXED, "BioRec1.mzML", "000/BioRec1.wiff", "BioRec2.mzML"], "",
                                       names, paired)
        self.assertEqual(keys[self.PREFIXED], keys["BioRec1.mzML"])
        self.assertEqual(keys[self.PREFIXED], keys["000/BioRec1.wiff"])
        self.assertNotEqual(keys[self.PREFIXED], keys["BioRec2.mzML"])
        self.assertFalse(verifier._two_samples(self.PREFIXED, "BioRec1.mzML", names, paired))
        # Two rows of one stem keep their own files, and a file of that stem no row names is neither's.
        two = [(0, "s1.raw", "A"), (1, "s1.mzml", "B")]
        self.assertTrue(verifier._two_samples("S1.raw", "S1.mzML", two))
        self.assertEqual(set(), verifier._tied_rows("S1.mzXML", two))

    def test_an_unrecorded_file_of_the_rows_own_stem_blocks_the_run(self) -> None:
        """The prefixed member runs for the row naming BioRec1.raw; the archive also holds BioRec1.mzML (a file the
        rule leaves unused, unrecorded) or 000/BioRec1.wiff (the file the rule uses: a vendor tie, '000/' first, so
        the wrong encoding runs), and no record names it."""
        for copy in ("BioRec1.mzML", "000/BioRec1.wiff"):
            with self.subTest(copy=copy), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, plain=(), converted=(), on_disk=(copy,))
                self._refused(d08._gate(unit), UNACCOUNTED + f"{copy} beside {self.PREFIXED}")

    def test_a_file_of_the_rows_own_stem_that_runs_beside_its_prefixed_member_blocks_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, plain=("BioRec1.mzML",), converted=())
            self._refused(d08._gate(unit), "1 sample(s) reach the run in more than one file, where the one encoding "
                                           f"rule uses exactly one file of a sample (clauses 1 and 2): {self.PREFIXED} "
                                           "and BioRec1.mzML")

    def test_the_rule_followed_for_a_file_of_the_rows_own_stem_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary, plain=(), converted=(), on_disk=("BioRec1.mzML",))
            _record(unit, _choice(self.PREFIXED, ("BioRec1.mzML", LOWER)))
            unit.prepare()
            self.assertEqual([], verifier._encoding_run_problems(unit.manifest, None)[0])

    # ---- (2) a left_out reason removes the file the rule names only where it applies -------------------------

    @staticmethod
    def _left_out(unit, path: str, reason: str) -> None:
        record = unit.manifest["unattributed_members"]
        record["left_out"] = [d08._left(path, reason)]
        record["left_out_count"] = 1

    def test_the_file_the_rule_names_left_out_for_a_reason_that_does_not_apply_blocks_the_run(self) -> None:
        """A/U_8.raw is the first of a tie with B/U_8.raw, which runs; U_8.raw outranks U_8.mzML, which runs."""
        cases = {
            "a shared archive's reason": ("B/U_8.raw", "A/U_8.raw", "shared_archive"),
            "a stem no two rows share": ("B/U_8.raw", "A/U_8.raw", "stem_of_several_sample_rows"),
            "a polarity the path does not state": ("B/U_8.raw", "A/U_8.raw", "polarity_token_contradicts_ion_mode"),
            "a declaration the unit does not make": ("B/U_8.raw", "A/U_8.raw", "not_named_by_the_catalog_declaration"),
            "a reason of another encoding": ("B/U_8.raw", "A/U_8.raw", "undecodable"),
            "a header reason with no exclusion": ("B/U_8.raw", "A/U_8.raw", "raw_header_unreadable"),
            "a lower encoding runs": ("U_8.mzML", "U_8.raw", "shared_archive"),
            "no reason": ("B/U_8.raw", "A/U_8.raw", ""),
        }
        for name, (running, left, reason) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, plain=(running,), on_disk=(left,))
                unit.manifest["project"]["ion_mode"] = "Positive"
                self._left_out(unit, left, reason)
                report = d08._gate(unit)
                self._refused(report, self.LEFT_OUT)
                self.assertIn(f"{left} ({reason or 'no reason'}: ", _check(report, "INP-1").detail)
                self.assertIn(f") while {running} runs", _check(report, "INP-1").detail)

    def test_a_left_out_file_after_the_one_that_runs_or_removed_by_its_reason_is_accounted_for(self) -> None:
        cases = {
            "after the one that runs": ("A/U_8.raw", "B/U_8.raw", "shared_archive", None),
            "an unreadable header on record": ("B/U_8.raw", "A/U_8.raw", "raw_header_unreadable",
                                               "raw_header_unreadable"),
        }
        for name, (running, left, reason, excluded) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                unit = d08._unit(temporary, plain=(running,), on_disk=(left,))
                self._left_out(unit, left, reason)
                if excluded:
                    unit.exclude(str(unit.data / left), excluded)
                inp1 = _check(d08._gate(unit), "INP-1")
                self.assertNotIn(self.LEFT_OUT, inp1.detail)
                self.assertNotIn(UNACCOUNTED, inp1.detail)

    def test_in_a_declared_unit_only_the_declaration_keeps_the_file_the_rule_names_out(self) -> None:
        """RAW/S1.raw, which no declaration names, outranks the declared S1.mzML: left out as undeclared it stays out
        (ReviewRound3Tests), left out for a shared archive it does not."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = ReviewRound3Tests()._declared_archive(temporary, left_out=True)
            unit.manifest["unattributed_members"]["left_out"] = [d08._left("RAW/S1.raw", "shared_archive")]
            unit.prepare()
            self._refused(unit.gate(), self.LEFT_OUT)

    def test_each_reason_removes_a_file_only_on_its_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            mzxml = {"source": {"relative_path": "A/U_9.mzXML"}, "status": "failed"}
            provenance = {"input_directory": str(root), "input_conversions": {"records": [mzxml]},
                          "project": {"ion_mode": "Positive"}}
            names = [(0, "s1.raw", "A"), (1, "s1.mzml", "B")]

            def why(path: str, reason: str, exclusions: "dict | None" = None, polarity: str = "Positive") -> str:
                return verifier._left_out_removal_problem(path, reason, provenance, str(root), polarity, names, {},
                                                          exclusions or {})

            self.assertEqual("", why("A/U_9.mzXML", "conversion_failed"))
            mzxml["status"] = "converted"
            self.assertNotEqual("", why("A/U_9.mzXML", "conversion_failed"))
            self.assertNotEqual("", why("B/U_9.mzXML", "conversion_failed"))
            self.assertEqual("", why("A/U_9.mzXML", "requires_conversion"))
            mzml = str(root / "A" / "U_9.mzML")
            key = verifier._path_key(mzml)
            self.assertEqual("", why("A/U_9.mzML", "undecodable", {key: (mzml, "unsupported_mzml_encoding")}))
            self.assertNotEqual("", why("A/U_9.mzML", "undecodable"))
            self.assertNotEqual("", why("A/U_9.raw", "undecodable", {key: (mzml, "undecodable")}))
            self.assertEqual("", why("NEG/U_9.raw", "polarity_token_contradicts_ion_mode"))
            self.assertNotEqual("", why("POS/U_9.raw", "polarity_token_contradicts_ion_mode"))
            self.assertNotEqual("", why("NEG/U_9.raw", "polarity_token_contradicts_ion_mode", polarity=""))
            self.assertEqual("", why("S1.mzXML", "stem_of_several_sample_rows"))
            self.assertNotEqual("", why("S1.raw", "stem_of_several_sample_rows"))
            self.assertNotEqual("", why("U_9.raw", "not_named_by_the_catalog_declaration"))
            self.assertNotEqual("", why("U_9.raw", "shared_archive"))


if __name__ == "__main__":
    unittest.main()
