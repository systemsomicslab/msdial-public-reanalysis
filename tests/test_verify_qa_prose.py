"""QA-1: the published QA text and table say what the assessment says, and no more.

QA-1 FAILs only on what it can establish: one of Interactive's own sentences (0.5.1 and later, or
the wording before 0.5.1) that contradicts the assessment, a whole sentence saying one criterion was
met, not met, not assessed or had a value against its status, a table row that contradicts the
record, and a record that contradicts itself. Interactive's QA text is nothing but its own
sentences, so any other sentence in it is a WARN for a person to read, and a PASS means nothing
else was written about QA.

The fixtures that matter most are real. INTERACTIVE_052 is what Interactive 0.5.2 wrote for
MTBLS2207's DIA run; INTERACTIVE_050 is what 0.5.0 wrote for the DDA run it superseded, which
recited QC precision, blank separation and carryover for a run with no QC and no Blank;
NO_QA_MATRIX is what an older run without a QA matrix wrote; WRITER holds 0.5.2's text for three
more assessments. Every FAIL case checks the message of the rule it names, so a case cannot pass
because another rule fired.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import textwrap
import time
import unittest
import zipfile
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify-run-invariants.py"
if "verify_run_invariants" in sys.modules:
    verifier = sys.modules["verify_run_invariants"]
else:
    _SPEC = importlib.util.spec_from_file_location("verify_run_invariants", _MODULE_PATH)
    assert _SPEC and _SPEC.loader
    verifier = importlib.util.module_from_spec(_SPEC)
    sys.modules["verify_run_invariants"] = verifier
    _SPEC.loader.exec_module(verifier)


SEVEN = [
    ("median_qc_rsd_percent", "Median QC feature RSD", "<=", 30.0, "%"),
    ("qc_features_rsd_le_30_percent", "Fraction of QC features with RSD <=30%", ">=", 0.7, "fraction"),
    ("median_qc_detection_rate", "Median QC detection rate", ">=", 0.8, "fraction"),
    ("sample_blank_ratio_ge_3", "Fraction of features with Sample/Blank >=3", ">=", 0.7, "fraction"),
    ("qc_pca_relative_dispersion", "QC PCA relative dispersion", "<=", 0.5, "ratio"),
    ("median_blank_carryover_ratio", "Median blank carryover ratio", "<=", 0.1, "fraction"),
    ("run_order_intensity_correlation", "Absolute run-order/intensity correlation", "abs<=", 0.3, "correlation"),
]
LABEL = {metric: label for metric, label, *_ in SEVEN}
SIX = "; ".join(label for _, label, *_ in SEVEN[:6])
ALL = "; ".join(label for _, label, *_ in SEVEN)

PROCESSING = (
    "MS-DIAL data processing\n\n"
    "Raw mass spectrometry data (5 analysis files) were processed using MS-DIAL Console version "
    "5.5.260926 through MS-DIAL Interactive version 0.5.2. Features were aligned with retention-time "
    "and MS1 tolerances of 0.1 min and 0.015 Da.\n\n"
)
CRITERIA_052 = (
    "Of 7 prespecified QA criteria, 1 could be evaluated (Absolute run-order/intensity correlation), "
    f"and 0 of them were met. The other 6 ({SIX}) could not be assessed because the run had 0 QC "
    "injection(s), where at least three are needed and no Blank files. Individual criteria and "
    "outcomes are reported in Supplementary Table S1."
)
REASON_052 = (" because the run had 0 QC injection(s), where at least three are needed and no Blank files")
INTERACTIVE_052 = {
    "methods": PROCESSING + "Quality assurance\n\nAnalytical quality was assessed from 5 files (5 study "
               "samples, 0 pooled QC samples, and 0 blanks). " + CRITERIA_052 + "\n",
    "results": "Quality assessment included 5 injections and 18098 aligned features. " + CRITERIA_052
               + " Criteria requiring review were: Absolute run-order/intensity correlation.\n",
    "statuses": {"run_order_intensity_correlation": "fail"},
    "values": {"run_order_intensity_correlation": -0.4988351017208908},
    "summary": {"sample_count": 5, "alignment_spot_count": 18098,
                "category_counts": {"Sample": 5, "QC": 0, "Blank": 0},
                "run_order_intensity_correlation": -0.4988351017208908, "median_qc_rsd_percent": None},
}
INTERACTIVE_050 = {
    "methods": PROCESSING + "Quality assurance\n\nAnalytical quality was assessed from 6 files (6 study "
               "samples, 0 pooled QC samples, and 0 blanks) using feature-intensity distributions, QC "
               "precision and detection rate, blank separation and carryover, PCA topology, "
               "analytical-order drift, MS/MS acquisition, raw signal-to-noise ratios, and "
               "internal-standard mass and retention-time errors where available. 1 of 1 prespecified, "
               "evaluable QA criteria were met; individual criteria and outcomes are reported in "
               "Supplementary Table S1.\n",
    "results": "Quality assessment included 6 injections and 8634 aligned features. Overall, 1 of 1 "
               "evaluable prespecified QA criteria were met.\n",
    "statuses": {"run_order_intensity_correlation": "pass"},
    "values": {"run_order_intensity_correlation": 0.20167250692886177},
    "summary": {"sample_count": 6, "alignment_spot_count": 8634,
                "category_counts": {"Sample": 6, "QC": 0, "Blank": 0}},
}
NO_QA_MATRIX = {
    "methods": PROCESSING + "Quality assurance\n\nNo LC-MS quality-assurance matrix was supplied when this "
               "report was generated; QA claims should be added after assessment.\n",
    "results": "Quality-assurance results were not generated because no LC-MS QA matrix was supplied.\n",
    "statuses": {},
    "values": {},
    "summary": None,
}
ASSESSED_VALUES = {"median_qc_rsd_percent": 14.25, "qc_features_rsd_le_30_percent": 0.81,
                   "median_qc_detection_rate": 0.93, "sample_blank_ratio_ge_3": 0.75,
                   "qc_pca_relative_dispersion": 0.2, "median_blank_carryover_ratio": 0.02,
                   "run_order_intensity_correlation": 0.45}
WRITER = {
    "all assessed": {
        "methods": "Quality assurance\n\nAnalytical quality was assessed from 12 files (6 study samples, 4 pooled "
                   f"QC samples, and 2 blanks). Of 7 prespecified QA criteria, 7 could be evaluated ({ALL}), and 6 "
                   "of them were met. Individual criteria and outcomes are reported in Supplementary Table S1.\n",
        "results": "Quality assessment included 12 injections and 900 aligned features. The median feature RSD "
                   "among QC injections was 14.2%. The median QC detection rate was 93.0%. QC relative dispersion "
                   "in the first two PCA dimensions was 0.200 compared with all displayed samples. Of 7 prespecified "
                   f"QA criteria, 7 could be evaluated ({ALL}), and 6 of them were met. Individual criteria and "
                   "outcomes are reported in Supplementary Table S1. Criteria requiring review were: Absolute "
                   "run-order/intensity correlation.\n",
        "statuses": {metric: "pass" for metric, *_ in SEVEN} | {"run_order_intensity_correlation": "fail"},
        "values": ASSESSED_VALUES,
        "summary": {"sample_count": 12, "alignment_spot_count": 900,
                    "category_counts": {"Sample": 6, "QC": 4, "Blank": 2}} | ASSESSED_VALUES,
    },
    "none evaluable": {
        "methods": "Quality assurance\n\nAnalytical quality was assessed from 4 files (4 study samples, 0 pooled QC "
                   "samples, and 0 blanks). None of the 7 prespecified QA criteria could be evaluated. The other 7 "
                   f"({ALL}) could not be assessed because the run had 0 QC injection(s), where at least three are "
                   "needed and no Blank files. Individual criteria and outcomes are reported in Supplementary Table S1.\n",
        "results": "Quality assessment included 4 injections and 50 aligned features. None of the 7 prespecified QA "
                   f"criteria could be evaluated. The other 7 ({ALL}) could not be assessed because the run had 0 QC "
                   "injection(s), where at least three are needed and no Blank files. Individual criteria and outcomes "
                   "are reported in Supplementary Table S1.\n",
        "statuses": {},
        "values": {},
        "summary": {"sample_count": 4, "alignment_spot_count": 50,
                    "category_counts": {"Sample": 4, "QC": 0, "Blank": 0}},
    },
    "no reason given": {
        "methods": "Quality assurance\n\nAnalytical quality was assessed from 10 files (5 study samples, 3 pooled QC "
                   "samples, and 2 blanks). Of 7 prespecified QA criteria, 6 could be evaluated (Median QC feature "
                   "RSD; Fraction of QC features with RSD <=30%; Fraction of features with Sample/Blank >=3; QC PCA "
                   "relative dispersion; Median blank carryover ratio; Absolute run-order/intensity correlation), and "
                   "6 of them were met. The other 1 (Median QC detection rate) could not be assessed. Individual "
                   "criteria and outcomes are reported in Supplementary Table S1.\n",
        "results": "",
        "statuses": {metric: "pass" for metric, *_ in SEVEN} | {"median_qc_detection_rate": "not_assessed"},
        "values": {key: value for key, value in ASSESSED_VALUES.items() if key != "median_qc_detection_rate"}
                  | {"run_order_intensity_correlation": 0.1},
        "summary": {"sample_count": 10, "alignment_spot_count": 300,
                    "category_counts": {"Sample": 5, "QC": 3, "Blank": 2}},
    },
}


def _checks(statuses: dict[str, str], values: dict | None = None, metrics=None) -> list[dict]:
    values = values or {}
    wanted = metrics or [metric for metric, *_ in SEVEN]
    return [
        {"metric": metric, "label": label, "value": values.get(metric), "operator": operator,
         "threshold": threshold, "unit": unit, "status": statuses.get(metric, "not_assessed")}
        for metric, label, operator, threshold, unit in SEVEN if metric in wanted
    ]


def _table(checks: list[dict], edit=None, extra: list[str] | None = None) -> str:
    """The Supplementary Table's QA rows as Interactive writes them."""
    lines = ["Section\tRecord\tParameter\tValue\tUnit or note\tSource"]
    for item in checks:
        rows = [
            ["Observed", "not recorded" if item["value"] is None else str(item["value"])],
            ["Criterion", f"{item['operator']} {item['threshold']}"],
            ["Assessment", item["status"]],
        ] + ([["Not assessed because", item["reason"]]] if item.get("reason") else [])
        if edit:
            rows = edit(item["label"], rows)
        for parameter, value in rows:
            lines.append(f"Quality assurance\t{item['label']}\t{parameter}\t{value}\t\tMS-DIAL Interactive")
    return "\n".join(lines + (extra or [])) + "\n"


class Workspace:
    def __init__(self, root: Path, checks: list, *, summary=None, methods: str | None = None,
                 results: str | None = None, table: str | None = None, assessment: dict | None = None) -> None:
        self.root = root
        self.output = root / "output"
        self.output.mkdir(parents=True)
        (root / "provenance").mkdir()
        dicts = [item for item in checks if isinstance(item, dict)]
        record = {
            "passed": sum(1 for item in dicts if item.get("status") == "pass"),
            "evaluated": sum(1 for item in dicts if item.get("status") != "not_assessed"),
            "checks": checks,
        }
        record.update(assessment or {})
        self.report = {"qa_assessment": record, "qa_report": {"summary": summary} if summary is not None else None}
        self.write_report()
        for name, text in (("MS_DIAL_Materials_and_Methods.txt", methods), ("MS_DIAL_QA_Results.txt", results),
                           ("Supplementary_Table_MS_DIAL.tsv", table)):
            if text is not None:
                (self.output / name).write_text(("﻿" if name.endswith(".tsv") else "") + text, encoding="utf-8")

    def write_report(self) -> None:
        (self.output / "MS_DIAL_publication_report.json").write_text(json.dumps(self.report), encoding="utf-8")

    def bundle(self, members: dict[str, str | bytes]) -> "Workspace":
        with zipfile.ZipFile(self.output / "MS_DIAL_publication_reporting_bundle.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in members.items():
                archive.writestr(name, text)
        return self

    def qa1(self):
        report = verifier.verify(self.root, "before-publish")
        matching = [check for check in report.checks if check.check_id == "QA-1"]
        assert len(matching) == 1, f"QA-1 ran {len(matching)} times"
        return matching[0]


def _real(root: Path, fixture: dict, *, methods=None, results=None, table=True, summary="real",
          edit=None, extra=None) -> Workspace:
    checks = _checks(fixture["statuses"], fixture["values"])
    return Workspace(
        root, checks,
        summary=fixture["summary"] if summary == "real" else summary,
        methods=fixture["methods"] if methods is None else methods,
        results=fixture["results"] if results is None else results,
        table=_table(checks, edit, extra) if table is True else (table or None),
    )


def _run(fixture: dict, **options):
    with tempfile.TemporaryDirectory() as directory:
        return _real(Path(directory), fixture, **options).qa1()


def _append(fixture: dict, sentence: str) -> str:
    return fixture["results"].rstrip() + " " + sentence + "\n"


def _joined(check, key: str) -> str:
    return " ".join(check.evidence.get(key, []))


class RealTextTests(unittest.TestCase):
    def test_what_interactive_0_5_2_wrote_passes(self) -> None:
        check = _run(INTERACTIVE_052)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual([LABEL[metric] for metric, *_ in SEVEN[:6]], check.evidence["not_assessed_names"])
        self.assertEqual(["MS_DIAL_Materials_and_Methods.txt", "MS_DIAL_QA_Results.txt"],
                         check.evidence["carrying_the_statement"])
        self.assertIn("because the run had 0 QC injection(s)", check.detail)

    def test_what_interactive_0_5_2_writes_for_other_assessments_passes(self) -> None:
        for name in ("all assessed", "none evaluable"):
            with self.subTest(name):
                check = _run(WRITER[name])
                self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_where_interactive_gives_no_reason_a_person_reads(self) -> None:
        check = _run(WRITER["no reason given"])

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("does not say why 1 criterion could not be assessed", _joined(check, "warns"))

    def test_the_wording_before_0_5_1_is_refused(self) -> None:
        check = _run(INTERACTIVE_050)

        self.assertEqual(verifier.FAIL, check.status)
        fails = _joined(check, "fails")
        self.assertIn("Methods.txt: says quality was assessed using the whole battery, but 6 of the criteria it "
                      "names were not assessed", fails)
        self.assertIn("Results.txt: counts only the 1 evaluable of 7 criteria", fails)

    def test_the_wording_before_0_5_1_is_true_when_every_criterion_was_assessed(self) -> None:
        fixture = WRITER["all assessed"]
        methods = ("Quality assurance\n\nAnalytical quality was assessed from 12 files (6 study samples, 4 pooled QC "
                   "samples, and 2 blanks) using feature-intensity distributions, QC precision and detection rate, "
                   "blank separation and carryover, PCA topology, analytical-order drift, MS/MS acquisition, raw "
                   "signal-to-noise ratios, and internal-standard mass and retention-time errors where available. "
                   "6 of 7 prespecified, evaluable QA criteria were met; individual criteria and outcomes are "
                   "reported in Supplementary Table S1.\n")
        check = _run(fixture, methods=methods, results="")

        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_the_older_counts_are_compared(self) -> None:
        fixture = WRITER["all assessed"]
        for sentence, message in {
            "Overall, 7 of 7 evaluable prespecified QA criteria were met.": "says 7 of 7 evaluable criteria were met",
            "Prespecified QA criteria could not be evaluated from the available sample types.":
                "says no prespecified criterion could be evaluated",
        }.items():
            with self.subTest(sentence):
                check = _run(fixture, methods="", results=sentence)
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_a_run_without_a_qa_matrix_passes_when_its_text_says_so(self) -> None:
        check = _run(NO_QA_MATRIX, table=False)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn("no QA matrix was supplied", check.detail)

    def test_saying_no_matrix_was_supplied_when_one_was_is_refused(self) -> None:
        check = _run(INTERACTIVE_052, results=NO_QA_MATRIX["results"])

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("says no QA matrix was supplied; the report carries its summary", _joined(check, "fails"))

    def test_the_battery_recited_beside_a_blanket_denial_is_refused(self) -> None:
        # What Interactive wrote before 0.5.1 when no criterion could be evaluated.
        methods = ("Quality assurance\n\nAnalytical quality was assessed from 4 files (4 study samples, 0 pooled "
                   "QC samples, and 0 blanks) using feature-intensity distributions, QC precision and detection "
                   "rate, blank separation and carryover, PCA topology, analytical-order drift, MS/MS acquisition, "
                   "raw signal-to-noise ratios, and internal-standard mass and retention-time errors where "
                   "available. Prespecified QA criteria could not be evaluated from the available sample types.\n")
        check = _run(WRITER["none evaluable"], methods=methods, results="")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("using the whole battery, but 7 of the criteria it names", _joined(check, "fails"))

    def test_the_battery_recited_before_the_qa_section_is_refused(self) -> None:
        battery = INTERACTIVE_050["methods"].split("Quality assurance\n\n")[1]
        methods = PROCESSING + battery + "\nQuality assurance\n\n" + INTERACTIVE_052["methods"].split(
            "Quality assurance\n\n")[1]
        check = _run(INTERACTIVE_052, methods=methods)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("using the whole battery", _joined(check, "fails"))

    def test_rewrapping_the_text_changes_nothing(self) -> None:
        def wrap(text: str, width: int) -> str:
            return "\n\n".join("\n".join(textwrap.wrap(part, width)) for part in text.split("\n\n")) + "\n"

        for width in (40, 60, 79):
            with self.subTest(width=width):
                check = _run(INTERACTIVE_052, methods=wrap(INTERACTIVE_052["methods"], width),
                             results=wrap(INTERACTIVE_052["results"], width))
                self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_crlf_text_is_read_as_written(self) -> None:
        check = _run(INTERACTIVE_052, methods=INTERACTIVE_052["methods"].replace("\n", "\r\n"))

        self.assertEqual(verifier.PASS, check.status, check.detail)


class StatementContradictionTests(unittest.TestCase):
    """Each case changes one fact in what 0.5.2 wrote, and is refused by the rule that fact concerns."""

    def test_every_count_list_and_reason_is_compared(self) -> None:
        results = INTERACTIVE_052["results"]
        methods = INTERACTIVE_052["methods"]
        blanks = dict(INTERACTIVE_052["summary"], category_counts={"Sample": 5, "QC": 0, "Blank": 2})
        cases = {
            "evaluated count": (dict(results=results.replace("1 could be evaluated", "2 could be evaluated")),
                                "Results.txt: says 2 of 7 prespecified criteria could be evaluated"),
            "prespecified count": (dict(results=results.replace("Of 7 prespecified", "Of 6 prespecified")),
                                   "says 1 of 6 prespecified criteria"),
            "met count": (dict(results=results.replace("0 of them were met", "1 of them was met")),
                          "could be evaluated and 1 met"),
            "evaluated list": (dict(results=results.replace(
                "evaluated (Absolute run-order/intensity correlation)",
                "evaluated (Absolute run-order/intensity correlation; Median QC detection rate)")),
                "as evaluated, which names Median QC detection rate, which it should not"),
            "other count": (dict(results=results.replace("The other 6 (", "The other 5 (")),
                            "Results.txt: says the other 5 could not be assessed"),
            "other list": (dict(results=results.replace("Median QC detection rate; ", "")),
                           "as not assessed, which leaves out Median QC detection rate"),
            "review list": (dict(results=results.replace(
                "review were: Absolute run-order/intensity correlation.", "review were: Median QC feature RSD.")),
                "as requiring review, which names Median QC feature RSD"),
            "QC count in the reason": (dict(results=results.replace("0 QC injection(s)", "2 QC injection(s)")),
                                       "gives 2 QC injection(s) as the reason; the QA matrix records 0"),
            "blanks in the reason": (dict(summary=blanks), "gives 'no Blank files' as the reason"),
            "injection count": (dict(results=results.replace("included 5 injections", "included 6 injections")),
                                "says 6 injections"),
            "feature count": (dict(results=results.replace("18098 aligned", "18099 aligned")),
                              "and 18099 aligned features"),
            "sample types": (dict(methods=methods.replace("(5 study samples, 0 pooled QC samples",
                                                          "(4 study samples, 1 pooled QC samples")),
                             "Methods.txt: says 5 files (4 study samples"),
            "none evaluable, but one was": (dict(results=results.replace(
                "Of 7 prespecified QA criteria, 1 could be evaluated (Absolute run-order/intensity correlation), "
                "and 0 of them were met.", "None of the 7 prespecified QA criteria could be evaluated.")),
                "says none of 7 prespecified criteria could be evaluated"),
        }
        for name, (options, message) in cases.items():
            with self.subTest(name):
                summary = options.pop("summary", "real")
                check = _run(INTERACTIVE_052, summary=summary, **options)
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_a_qc_reason_where_three_were_present_is_refused(self) -> None:
        fixture = WRITER["no reason given"]
        methods = fixture["methods"].replace(
            "could not be assessed.", "could not be assessed because the run had 3 QC injection(s), where at least "
            "three are needed.")
        check = _run(fixture, methods=methods)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("'where at least three are needed', as the reason", _joined(check, "fails"))

    def test_the_value_sentences_are_compared_as_numbers(self) -> None:
        results = WRITER["all assessed"]["results"]
        for name, (text, verdict, message) in {
            "RSD": (results.replace("was 14.2%.", "was 12.0%."), verifier.FAIL,
                    "gives 12.0 as the value of 'Median QC feature RSD'"),
            "detection rate": (results.replace("was 93.0%.", "was 99.0%."), verifier.FAIL, "gives 99.0 as the value"),
            "dispersion": (results.replace("was 0.200 compared", "was 0.100 compared"), verifier.FAIL,
                           "gives 0.100 as the value"),
            "half up": (results.replace("was 14.2%.", "was 14.3%."), verifier.PASS, ""),
            "fewer digits": (results.replace("was 0.200 compared", "was 0.2 compared"), verifier.PASS, ""),
        }.items():
            with self.subTest(name):
                check = _run(WRITER["all assessed"], results=text)
                self.assertEqual(verdict, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_a_value_sentence_for_an_unassessed_criterion_is_refused(self) -> None:
        results = INTERACTIVE_052["results"].replace(
            "aligned features. ", "aligned features. The median feature RSD among QC injections was 12.3%. ")
        check = _run(INTERACTIVE_052, results=results)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("gives 12.3 as the value of 'Median QC feature RSD', which was not assessed", _joined(check, "fails"))

    def test_lists_are_read_as_lists(self) -> None:
        results = INTERACTIVE_052["results"]
        commas = results.replace(SIX, SIX.replace("; Median blank", " and Median blank").replace(";", ","))
        for name, (text, verdict) in {
            "commas and a final and": (commas, verifier.PASS),
            "a name QA-1 cannot match": (results.replace("Median QC detection rate;", "Median QC detektion rate;"),
                                         verifier.WARN),
            "none requiring review": (results.replace("review were: Absolute run-order/intensity correlation.",
                                                      "review were: none."), verifier.FAIL),
        }.items():
            with self.subTest(name):
                check = _run(INTERACTIVE_052, results=text)
                self.assertEqual(verdict, check.status, check.detail)

    def test_none_requiring_review_is_true_when_nothing_failed(self) -> None:
        fixture = WRITER["no reason given"]
        check = _run(fixture, methods=fixture["methods"].replace(
            "Individual criteria", "Criteria requiring review were: none. Individual criteria"))

        self.assertEqual(verifier.WARN, check.status, check.detail)   # the missing reason, nothing else
        self.assertEqual(0, check.evidence["fail_count"])

    def test_an_assessment_that_contradicts_its_own_checks_is_refused(self) -> None:
        checks = _checks(INTERACTIVE_052["statuses"], INTERACTIVE_052["values"])
        with tempfile.TemporaryDirectory() as directory:
            check = Workspace(Path(directory), checks, summary=INTERACTIVE_052["summary"],
                              results=INTERACTIVE_052["results"], assessment={"evaluated": 7}).qa1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("The assessment contradicts itself", check.detail)


class ClaimTests(unittest.TestCase):
    """A whole sentence that says one thing of one criterion is read as a claim; nothing else is."""

    def test_a_sentence_giving_the_wrong_outcome_is_refused(self) -> None:
        cases = {
            "a failed criterion met": (INTERACTIVE_052, "Absolute run-order/intensity correlation was met.",
                                       "was met; the assessment failed it"),
            "an unassessed criterion met": (INTERACTIVE_052, "Median QC feature RSD was met.",
                                            "was met; the assessment did not assess it"),
            "an unassessed criterion measured": (INTERACTIVE_052, "The median QC feature RSD was 12%.",
                                                 "gives a value for 'Median QC feature RSD', which was not assessed"),
            "a passed criterion not met": (WRITER["all assessed"], "Median QC feature RSD was not met.",
                                           "was not met; the assessment passed it"),
            "an assessed criterion not assessed": (WRITER["all assessed"],
                                                   "Median blank carryover ratio could not be assessed.",
                                                   "could not be assessed; the assessment passed it"),
            "a wrong value": (WRITER["all assessed"], "QC PCA relative dispersion was 0.6.",
                              "gives 0.6 for 'QC PCA relative dispersion'"),
        }
        for name, (fixture, sentence, message) in cases.items():
            with self.subTest(name):
                check = _run(fixture, results=_append(fixture, sentence))
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_any_other_sentence_is_for_a_person_to_read(self) -> None:
        sentences = [
            "Absolute run-order/intensity correlation was not met.",           # true, but not Interactive's
            "Absolute run-order/intensity correlation (r = -0.50) passed its acceptance limit.",
            "The acceptance limit for Median QC feature RSD is 30%.",
            "Whether Median QC feature RSD was met could not be determined.",
            "The Median QC feature RSD metric could not be computed.",
            "QA status: pass.",
            "The run met all acceptance criteria.",
            "None of the 7 prespecified QA criteria failed.",
            "Reproducibility of the pooled samples was excellent.",
        ]
        for sentence in sentences:
            with self.subTest(sentence):
                check = _run(INTERACTIVE_052, results=_append(INTERACTIVE_052, sentence))
                self.assertEqual(verifier.WARN, check.status, check.detail)
                self.assertIn("in its QA text are not Interactive's; read them", _joined(check, "warns"))

    def test_a_claim_inside_the_reason_is_not_taken_for_the_reason(self) -> None:
        results = INTERACTIVE_052["results"].replace(
            "no Blank files.", "no Blank files, and Absolute run-order/intensity correlation was met.")
        check = _run(INTERACTIVE_052, results=results)

        self.assertIn(check.status, (verifier.WARN, verifier.FAIL))
        self.assertNotEqual(verifier.PASS, check.status)


class OtherTextTests(unittest.TestCase):
    def test_honest_rewrites_are_for_a_person_to_read(self) -> None:
        rewrites = {
            "list after a colon": "Only the run-order/intensity correlation could be evaluated, and it failed. The "
                                  f"following could not be assessed because the run had no QC or blank injections: {SIX}.",
            "spelled-out counts": "Six of the seven criteria were not assessable because no pooled QC or blank "
                                  "injections were acquired; the seventh failed.",
        }
        for name, text in rewrites.items():
            with self.subTest(name):
                check = _run(INTERACTIVE_052, results=text)
                self.assertEqual(verifier.WARN, check.status, check.detail)
                self.assertIn("in its QA text are not Interactive's; read them", _joined(check, "warns"))

    def test_a_paraphrase_alone_is_never_a_pass(self) -> None:
        check = _run(INTERACTIVE_052, methods="", results="Reproducibility of the pooled samples was excellent.",
                     table=False)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("no text carries the statement", _joined(check, "warns"))

    def test_the_count_without_the_sentence_naming_the_rest_is_for_a_person(self) -> None:
        results = INTERACTIVE_052["results"].replace(
            f" The other 6 ({SIX}) could not be assessed{REASON_052}.", "")
        check = _run(INTERACTIVE_052, methods="", results=results, table=False)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("gives Interactive's count but not its sentence naming the 6", _joined(check, "warns"))

    def test_qa_before_the_methods_qa_section_is_for_a_person(self) -> None:
        for phrase in ("QC precision", "blank separation", "carryover", "PCA topology", "analytical-order drift",
                       "QC detection rate", "RSD <=30%", "acceptance criteria", "prespecified"):
            with self.subTest(phrase):
                methods = INTERACTIVE_052["methods"].replace(
                    "0.015 Da.", f"0.015 Da. The {phrase} was reviewed with the laboratory.")
                check = _run(INTERACTIVE_052, methods=methods)
                self.assertEqual(verifier.WARN, check.status, check.detail)
                self.assertIn("speaks of QA before its QA section", _joined(check, "warns"))

    def test_a_methods_without_its_heading_is_for_a_person(self) -> None:
        methods = INTERACTIVE_052["methods"].replace("Quality assurance\n\n", "")
        check = _run(INTERACTIVE_052, methods=methods)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("has no 'Quality assurance' heading", _joined(check, "warns"))

    def test_a_text_silent_on_qa_beside_one_that_carries_the_statement_passes(self) -> None:
        check = _run(INTERACTIVE_052, methods=PROCESSING)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(["MS_DIAL_QA_Results.txt"], check.evidence["carrying_the_statement"])

    def test_texts_silent_on_qa_are_never_a_pass(self) -> None:
        check = _run(INTERACTIVE_052, methods=PROCESSING, results="MS-DIAL 5.5 was used.", table=False)

        self.assertEqual(verifier.WARN, check.status, check.detail)

    def test_reasons_that_do_not_fit_the_criteria_are_for_a_person(self) -> None:
        # Only a blank-based criterion missing, yet the reason names the QC count too: Interactive's own
        # wording when the run had fewer than three QC.
        statuses = {metric: "pass" for metric, *_ in SEVEN} | {"median_blank_carryover_ratio": "not_assessed"}
        values = dict(ASSESSED_VALUES, median_blank_carryover_ratio=None)
        fixture = {
            "statuses": statuses, "values": values,
            "summary": {"sample_count": 8, "alignment_spot_count": 90,
                        "category_counts": {"Sample": 6, "QC": 2, "Blank": 0}},
            "methods": "",
            "results": "Quality assessment included 8 injections and 90 aligned features. Of 7 prespecified QA criteria, "
                       "6 could be evaluated (" + "; ".join(LABEL[m] for m, *_ in SEVEN if m != "median_blank_carryover_ratio")
                       + "), and 6 of them were met. The other 1 (Median blank carryover ratio) could not be assessed "
                       "because the run had 2 QC injection(s), where at least three are needed and no Blank files.",
        }
        check = _run(fixture, table=False)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("gives the QC count as the reason, but no QC-based criterion", _joined(check, "warns"))

    def test_counts_with_nothing_to_compare_are_for_a_person(self) -> None:
        check = _run(INTERACTIVE_052, summary={"category_counts": {"Sample": 5}})

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("which the report has no figure to compare with", _joined(check, "warns"))


class TableTests(unittest.TestCase):
    def test_rows_that_contradict_the_record_are_refused(self) -> None:
        def row(metric, parameter, value):
            def edit(label, rows):
                if label != LABEL[metric]:
                    return rows
                return [[name, value if name == parameter else old] for name, old in rows]
            return edit

        def duplicate(label, rows):
            return rows + [["Assessment", "pass"]] if label == LABEL["median_qc_rsd_percent"] else rows

        cases = {
            "assessment": (row("median_qc_rsd_percent", "Assessment", "pass"), "assesses 'Median QC feature RSD' as 'pass'"),
            "observed for an unassessed criterion": (row("median_blank_carryover_ratio", "Observed", "0.05"),
                                                     "gives 0.05 as observed for 'Median blank carryover ratio'"),
            "observed value": (row("run_order_intensity_correlation", "Observed", "-0.2"),
                               "gives -0.2 as observed for 'Absolute run-order/intensity correlation'"),
            "a duplicate row": (duplicate, "assesses 'Median QC feature RSD' as 'pass'"),
            "a changed criterion": (row("run_order_intensity_correlation", "Criterion", "abs<= 0.6"),
                                    "as the criterion for 'Absolute run-order/intensity correlation'"),
        }
        for name, (edit, message) in cases.items():
            with self.subTest(name):
                check = _run(INTERACTIVE_052, edit=edit)
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_the_observed_metric_rows_are_compared_with_the_qa_matrix(self) -> None:
        for name, row, message in (
            ("an object", "category_counts\t{\"Sample\":5,\"QC\":3,\"Blank\":0}", "as category_counts"),
            ("a count", "alignment_spot_count\t18000", "gives 18000 as alignment_spot_count"),
            ("a value where there is none", "median_qc_rsd_percent\t12.5", "gives 12.5 as median_qc_rsd_percent"),
        ):
            with self.subTest(name):
                check = _run(INTERACTIVE_052, extra=[f"Quality assurance\tObserved metric\t{row}\t\tx"])
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_honest_formatting_of_the_table_passes(self) -> None:
        def edit(label, rows):
            out = []
            for name, value in rows:
                if name == "Observed" and label == LABEL["run_order_intensity_correlation"]:
                    value = "−0,4988"
                if name == "Assessment":
                    value = {"not_assessed": "Not assessed", "fail": "Failed"}.get(value, value)
                out.append([name, value])
            return out

        check = _run(INTERACTIVE_052, edit=edit)
        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_a_fraction_printed_as_a_percent_passes(self) -> None:
        def edit(label, rows):
            return [[name, "93.0%" if name == "Observed" and label == LABEL["median_qc_detection_rate"] else value]
                    for name, value in rows]

        check = _run(WRITER["all assessed"], edit=edit)
        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_rows_qa1_cannot_place_are_for_a_person(self) -> None:
        def drop(label, rows):
            return [row for row in rows if not (label == LABEL["qc_pca_relative_dispersion"] and row[0] == "Assessment")]

        def extra_parameter(label, rows):
            return rows + [["Outcome", "met"]] if label == LABEL["median_qc_rsd_percent"] else rows

        for name, options, message in (
            ("no assessment row", dict(edit=drop), "has no Assessment row for 'QC PCA relative dispersion'"),
            ("an unknown parameter", dict(edit=extra_parameter), "a row QA-1 does not read"),
            ("an unknown record", dict(extra=["Quality assurance\tMedian IS mass error\tAssessment\tpass\t\tx"]),
             "for 'median is mass error', which the assessment does not list"),
            ("another QA section", dict(extra=["QA summary\tMedian QC feature RSD\tAssessment\tpass\t\tx"]),
             "which QA-1 does not read as the QA section"),
        ):
            with self.subTest(name):
                check = _run(INTERACTIVE_052, **options)
                self.assertEqual(verifier.WARN, check.status, check.detail)
                self.assertIn(message, _joined(check, "warns"))

    def test_a_table_with_no_qa_rows_is_for_a_person_when_the_text_cites_it(self) -> None:
        check = _run(INTERACTIVE_052, table="Section\tRecord\tParameter\tValue\tUnit or note\tSource\n")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("carries no Quality assurance rows", _joined(check, "warns"))

    def test_a_table_contradiction_is_reported_with_no_prose_to_read(self) -> None:
        def passed(label, rows):
            return [[name, "pass" if name == "Assessment" else value] for name, value in rows]

        checks = _checks(INTERACTIVE_052["statuses"], INTERACTIVE_052["values"])
        with tempfile.TemporaryDirectory() as directory:
            check = Workspace(Path(directory), checks, summary=INTERACTIVE_052["summary"],
                              table=_table(checks, passed)).qa1()

        self.assertEqual(verifier.FAIL, check.status)


class BundleTests(unittest.TestCase):
    def test_every_copy_in_the_bundle_is_read(self) -> None:
        for member in ("MS_DIAL_Materials_and_Methods.txt", "old/MS_DIAL_Materials_and_Methods.txt",
                       "ms_dial_materials_and_methods.TXT"):
            with self.subTest(member), tempfile.TemporaryDirectory() as directory:
                check = _real(Path(directory), INTERACTIVE_052).bundle({member: INTERACTIVE_050["methods"]}).qa1()
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(f"MS_DIAL_publication_reporting_bundle.zip:{member}", check.evidence["read"])

    def test_an_identical_copy_is_not_read_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _real(Path(directory), INTERACTIVE_052)
            workspace.bundle({"MS_DIAL_Materials_and_Methods.txt": INTERACTIVE_052["methods"].replace("\n", "\r\n"),
                              "copy/MS_DIAL_QA_Results.txt": INTERACTIVE_052["results"],
                              "MS_DIAL_publication_report.json": json.dumps(workspace.report)})
            check = workspace.qa1()

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(3, len(check.evidence["read"]))

    def test_a_copy_or_record_that_differs_is_for_a_person(self) -> None:
        changed = {"qa_assessment": {"status": "pass", "passed": 1, "evaluated": 1,
                                     "checks": _checks(INTERACTIVE_052["statuses"], INTERACTIVE_052["values"])},
                   "qa_report": {"summary": INTERACTIVE_052["summary"]}}
        cases = {
            "prose": ({"MS_DIAL_QA_Results.txt": INTERACTIVE_052["results"].strip() + " Reviewed by the authors.\n"},
                      "differs from the MS_DIAL_QA_Results.txt beside it"),
            "record": ({"MS_DIAL_publication_report.json": json.dumps(changed)}, "carries a different assessment"),
            "another name": ({"MS_DIAL_QA_Results.md": INTERACTIVE_050["results"]}, "looks like a QA text under another name"),
        }
        for name, (members, message) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                check = _real(Path(directory), INTERACTIVE_052).bundle(members).qa1()
                self.assertIn(message, _joined(check, "warns"))
                self.assertNotEqual(verifier.PASS, check.status)

    def test_a_member_that_cannot_be_read_is_named_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _real(Path(directory), INTERACTIVE_052)
            workspace.bundle({"MS_DIAL_QA_Results.txt": INTERACTIVE_052["results"] * 20})
            path = workspace.output / "MS_DIAL_publication_reporting_bundle.zip"
            data = bytearray(path.read_bytes())
            data[60:90] = b"\x00" * 30  # inside the deflated member
            path.write_bytes(bytes(data))
            check = workspace.qa1()

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("could not be read", _joined(check, "warns"))


class ReadingTests(unittest.TestCase):
    def test_utf16_and_utf32_with_a_bom_are_read(self) -> None:
        for encoding in ("utf-16", "utf-32"):
            with self.subTest(encoding), tempfile.TemporaryDirectory() as directory:
                workspace = _real(Path(directory), INTERACTIVE_052)
                (workspace.output / "MS_DIAL_QA_Results.txt").write_bytes(INTERACTIVE_052["results"].encode(encoding))
                check = workspace.qa1()
                self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_text_that_is_not_utf8_is_named_and_not_judged(self) -> None:
        for name, data, message in (("latin-1", b"Quality \xe9t\xe9 assessed \xff", "is not UTF-8 text"),
                                    ("utf-16 without a bom", INTERACTIVE_050["results"].encode("utf-16-le"),
                                     "holds NUL characters")):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                workspace = _real(Path(directory), INTERACTIVE_052)
                (workspace.output / "MS_DIAL_QA_Results.txt").write_bytes(data)
                check = workspace.qa1()
                self.assertEqual(verifier.WARN, check.status, check.detail)
                self.assertIn(message, _joined(check, "warns"))

    def test_no_text_to_read_is_not_evaluable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _real(Path(directory), INTERACTIVE_052, methods="", results="", table=True)
            for name in ("MS_DIAL_Materials_and_Methods.txt", "MS_DIAL_QA_Results.txt"):
                (workspace.output / name).unlink()
            check = workspace.qa1()

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertTrue(check.required)

    def test_empty_texts_are_not_evaluable(self) -> None:
        check = _run(INTERACTIVE_052, methods="", results="  \n", table=False)

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)

    def test_an_assessment_qa1_cannot_read_is_not_evaluable(self) -> None:
        good = _checks(INTERACTIVE_052["statuses"], INTERACTIVE_052["values"])
        cases = {
            "no criteria": [],
            "an unknown status": [dict(good[0], status="review")] + good[1:],
            "a missing status": [dict(good[0], status=None)] + good[1:],
            "no label": [dict(good[0], label=None)] + good[1:],
            "a label with a semicolon": [dict(good[0], label="RSD; median")] + good[1:],
            "a shared label": [good[0], dict(good[1], label=good[0]["label"])] + good[2:],
            "an entry that is not an object": [1] + good,
        }
        for name, checks in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                check = Workspace(Path(directory), checks, summary=INTERACTIVE_052["summary"],
                                  results=INTERACTIVE_052["results"]).qa1()
                self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)

    def test_malformed_records_are_a_verdict_not_a_traceback(self) -> None:
        odd = [1, "x", None, {"metric": None, "status": None}, {"label": ["x"], "status": 3},
               {"label": "Median QC feature RSD", "status": "not_assessed", "value": {"a": 1}}]
        good = _checks(INTERACTIVE_052["statuses"], INTERACTIVE_052["values"])
        summaries = {"a list": [1, 2], "string counts": {"category_counts": {"QC": "3", "Blank": "x"}},
                     "huge numbers": {"sample_count": 1e400, "alignment_spot_count": 10 ** 400,
                                      "category_counts": {"QC": float("inf")}},
                     "deep": {"category_counts": json.loads("[" * 50 + "]" * 50)}}
        prose = ("Of 7 prespecified QA criteria (unbalanced, 1 could be evaluated. QC precision ((( was fine. "
                 + "9" * 5000 + " QC injection(s), where at least three are needed.")
        for checks in (odd, good):
            for name, summary in summaries.items():
                with self.subTest(name, odd=checks is odd), tempfile.TemporaryDirectory() as directory:
                    workspace = Workspace(Path(directory), checks, results=prose, methods=prose,
                                          table="\x00\x01garbage\tno\theader\n")
                    workspace.report["qa_report"] = {"summary": summary}
                    workspace.write_report()
                    (workspace.output / "MS_DIAL_publication_reporting_bundle.zip").write_bytes(b"not a zip")
                    check = workspace.qa1()
                    self.assertIn(check.status, (verifier.PASS, verifier.WARN, verifier.FAIL, verifier.NOT_EVALUABLE))

    def test_deep_nesting_is_a_verdict_not_a_traceback(self) -> None:
        deep = "[" * 100000 + "]" * 100000
        with tempfile.TemporaryDirectory() as directory:
            workspace = _real(Path(directory), INTERACTIVE_052,
                              extra=[f"Quality assurance\tObserved metric\tcategory_counts\t{deep}\t\tx"])
            first = workspace.qa1()
            workspace.bundle({"MS_DIAL_publication_report.json": deep})
            second = workspace.qa1()
            (workspace.output / "MS_DIAL_publication_report.json").write_text(deep, encoding="utf-8")
            third = workspace.qa1()

        self.assertEqual(verifier.WARN, first.status, first.detail)
        self.assertNotEqual(verifier.PASS, second.status)
        self.assertEqual(verifier.NOT_EVALUABLE, third.status)

    def test_long_adversarial_text_is_read_in_bounded_time(self) -> None:
        texts = {
            "repeated qualifiers": "all prespecified, evaluable, " * 4000,
            "repeated labels": ("Median QC feature RSD and " * 4000) + "could not be assessed",
            "parentheses": "(" * 20000 + "The other 6 (" + "x; " * 10000,
            "review leads": "Criteria requiring review were: " * 4000,
            "of counts": "1 of 1 " * 15000,
            "sentences": "Median QC feature RSD was met. " * 4000,
        }
        for name, text in texts.items():
            with self.subTest(name):
                began = time.perf_counter()
                _run(INTERACTIVE_052, results=text, table=False)
                self.assertLess(time.perf_counter() - began, 10.0)


class RuleMessageTests(unittest.TestCase):
    """Each rule, by its own message: a case where another rule gives the same verdict cannot hide it."""

    def assertRule(self, check, verdict: str, key: str, message: str) -> None:
        self.assertEqual(verdict, check.status, check.detail)
        self.assertIn(message, _joined(check, key))

    def test_the_no_matrix_sentence_beside_evaluated_criteria_is_refused(self) -> None:
        check = _run(INTERACTIVE_052, summary=None, results=NO_QA_MATRIX["results"])
        self.assertRule(check, verifier.FAIL, "fails", "says no QA matrix was supplied; the assessment evaluated 1")

    def test_a_claim_before_the_qa_section_is_refused(self) -> None:
        methods = INTERACTIVE_052["methods"].replace("0.015 Da.", "0.015 Da. Absolute run-order/intensity correlation was met.")
        check = _run(INTERACTIVE_052, methods=methods)
        self.assertRule(check, verifier.FAIL, "fails", "Methods.txt: says 'Absolute run-order/intensity correlation' was met")

    def test_a_phrase_that_names_one_criterion_only(self) -> None:
        methods = INTERACTIVE_052["methods"].replace("0.015 Da.", "0.015 Da. The RSD among QC injections was reviewed.")
        check = _run(INTERACTIVE_052, methods=methods)
        self.assertRule(check, verifier.WARN, "warns", "speaks of QA before its QA section")

    def test_names_in_a_list_that_qa1_cannot_match(self) -> None:
        results = INTERACTIVE_052["results"]
        for name, text, message in (
            ("evaluated", results.replace("evaluated (Absolute run-order/intensity correlation)",
                                          "evaluated (Absolute run order intensity correlation)"),
             "its list of evaluated criteria names"),
            ("not assessed", results.replace("Median QC detection rate;", "Median QC detektion rate;"),
             "its list of criteria not assessed names"),
            ("review", results.replace("review were: Absolute run-order/intensity correlation.",
                                       "review were: run order correlation."),
             "its list of criteria requiring review names"),
        ):
            with self.subTest(name):
                self.assertRule(_run(INTERACTIVE_052, results=text), verifier.WARN, "warns", message)

    def test_reasons_that_do_not_fit(self) -> None:
        # A QC-based criterion missing, blanks present, yet the reason names missing blanks.
        statuses = {metric: "pass" for metric, *_ in SEVEN} | {"median_qc_detection_rate": "not_assessed"}
        values = dict(ASSESSED_VALUES, median_qc_detection_rate=None)
        base = {
            "statuses": statuses, "values": values, "methods": "",
            "summary": {"sample_count": 9, "alignment_spot_count": 90,
                        "category_counts": {"Sample": 4, "QC": 0, "Blank": 0}},
        }
        lead = ("Quality assessment included 9 injections and 90 aligned features. Of 7 prespecified QA criteria, 6 could "
                "be evaluated (" + "; ".join(LABEL[m] for m, *_ in SEVEN if m != "median_qc_detection_rate")
                + "), and 6 of them were met. The other 1 (Median QC detection rate) could not be assessed because the "
                "run had ")
        for name, reason, message in (
            ("blanks named, none missing", "0 QC injection(s), where at least three are needed and no Blank files.",
             "gives the missing blanks as the reason, but no blank-based criterion"),
            ("QC count left out", "no Blank files.", "does not give the QC count as a reason, although the run had 0 QC"),
        ):
            with self.subTest(name):
                check = _run(dict(base, results=lead + reason), table=False)
                self.assertRule(check, verifier.WARN, "warns", message)

    def test_value_sentences_qa1_cannot_compare(self) -> None:
        results = WRITER["all assessed"]["results"]
        cases = (
            ("an unlisted criterion", WRITER["all assessed"] | {"values": {k: v for k, v in ASSESSED_VALUES.items()}},
             results, "gives a value for median_qc_rsd_percent, which the assessment does not list",
             ["qc_features_rsd_le_30_percent", "median_qc_detection_rate", "sample_blank_ratio_ge_3",
              "qc_pca_relative_dispersion", "median_blank_carryover_ratio", "run_order_intensity_correlation"]),
            ("not a number, not assessed", INTERACTIVE_052, INTERACTIVE_052["results"].replace(
                "aligned features. ", "aligned features. The median QC detection rate was nan%. "),
             "prints 'nan' as the value of 'Median QC detection rate'", None),
            ("not a number, assessed", WRITER["all assessed"], results.replace("was 93.0%.", "was high%."),
             "gives 'high' as the value of 'Median QC detection rate'", None),
        )
        for name, fixture, text, message, metrics in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                checks = _checks(fixture["statuses"], fixture["values"], metrics=metrics)
                summary = fixture["summary"]
                if metrics:
                    total = len(checks)
                    text = text.replace("Of 7 prespecified QA criteria, 7 could be evaluated (" + ALL + "), and 6",
                                        f"Of {total} prespecified QA criteria, {total} could be evaluated ("
                                        + "; ".join(item["label"] for item in checks) + "), and 5")
                check = Workspace(Path(directory), checks, summary=summary, results=text, methods="").qa1()
                self.assertIn(message, _joined(check, "warns"))
                self.assertNotEqual(verifier.PASS, check.status)

    def test_table_rows_qa1_cannot_read(self) -> None:
        def cell(metric, parameter, value):
            def edit(label, rows):
                if label != LABEL[metric]:
                    return rows
                return [[name, value if name == parameter else old] for name, old in rows]
            return edit

        def no_rows(label, rows):
            return [] if label == LABEL["qc_pca_relative_dispersion"] else rows

        cases = (
            ("no rows", dict(edit=no_rows), "has no rows for 'QC PCA relative dispersion'"),
            ("an assessment word", dict(edit=cell("median_qc_rsd_percent", "Assessment", "maybe")),
             "assesses 'Median QC feature RSD' as 'maybe', which QA-1 cannot read"),
            ("observed for an unassessed criterion", dict(edit=cell("median_qc_rsd_percent", "Observed", "pending")),
             "gives 'pending' as observed for 'Median QC feature RSD', which was not assessed"),
            ("observed not a number", dict(edit=cell("run_order_intensity_correlation", "Observed", "high")),
             "gives 'high' as observed for 'Absolute run-order/intensity correlation', which QA-1 cannot read"),
            ("criterion not readable", dict(edit=cell("run_order_intensity_correlation", "Criterion", "see text")),
             "gives 'see text' as the criterion for 'Absolute run-order/intensity correlation', which QA-1 cannot read"),
            ("an object that is not JSON", dict(extra=["Quality assurance\tObserved metric\tcategory_counts\tSample 5; QC 0\t\tx"]),
             "gives 'Sample 5; QC 0' as category_counts, which QA-1 cannot read"),
            ("a count that is not a number", dict(extra=["Quality assurance\tObserved metric\tsample_count\tfive\t\tx"]),
             "gives 'five' as sample_count, which QA-1 cannot read as a number"),
            ("a word where the summary records none",
             dict(extra=["Quality assurance\tObserved metric\tmedian_qc_rsd_percent\tpending\t\tx"]),
             "gives 'pending' as median_qc_rsd_percent, which the QA matrix records as none"),
        )
        for name, options, message in cases:
            with self.subTest(name):
                self.assertRule(_run(INTERACTIVE_052, **options), verifier.WARN, "warns", message)

    def test_a_text_about_qa_without_the_statement_beside_one_with_it(self) -> None:
        check = _run(INTERACTIVE_052, results="Six criteria were not assessable.")
        self.assertRule(check, verifier.WARN, "warns", "MS_DIAL_QA_Results.txt: speaks of QA without the statement")


class RoundThreeTests(unittest.TestCase):
    """What the third review found, one case each."""

    def test_qa_words_before_the_qa_section_are_read(self) -> None:
        for sentence in ("The data passed all quality checks.", "The run passed QC.",
                         "The median feature RSD among pooled QC injections was 8.2%.",
                         "Pooled samples showed good reproducibility.", "No drift was seen."):
            with self.subTest(sentence):
                methods = INTERACTIVE_052["methods"].replace("0.015 Da.", "0.015 Da. " + sentence)
                check = _run(INTERACTIVE_052, methods=methods)
                self.assertEqual(verifier.WARN, check.status, check.detail)
                self.assertIn("speaks of QA before its QA section", _joined(check, "warns"))

    def test_a_file_name_is_not_qa_vocabulary(self) -> None:
        # The automatic RT-correction paragraph Interactive writes names its reference file, which is
        # often a pooled QC.
        rt = ("\n\nAfter peak detection and annotation on the original retention-time axis, MS-DIAL learned "
              "distributed anchor features. The retained Console audit records reference file pooled QC-01, which "
              "defines the axis and keeps its measured retention times. Of the other 4 audited file(s), 4 were "
              "corrected from their own anchors, 0 Blank file(s) took an interpolated or nearest-sample model, and 0 "
              "kept their original retention times. Aligned feature retention times, including those exported in "
              "mzTab-M, are therefore on the retention-time axis of reference file pooled QC-01; per-file peak lists "
              "keep the measured retention times. Library spectra came from QC-02.mzML.")
        methods = INTERACTIVE_052["methods"].replace("0.015 Da.", "0.015 Da." + rt)
        check = _run(INTERACTIVE_052, methods=methods)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        unmasked = methods.replace("reference file pooled QC-01", "the file pooled QC-01")
        self.assertEqual(verifier.WARN, _run(INTERACTIVE_052, methods=unmasked).status)

    def test_symbols_left_beside_the_statement_are_read(self) -> None:
        check = _run(INTERACTIVE_052, results=_append(INTERACTIVE_052, "\u2713 \u2713"))

        self.assertEqual(verifier.WARN, check.status, check.detail)

    def test_a_lower_case_clause_after_a_colon_is_not_interactives(self) -> None:
        sentence = ("Two groups: the other 2 (Median QC feature RSD; Median QC detection rate) could not be assessed.")
        check = _run(INTERACTIVE_052, results=_append(INTERACTIVE_052, sentence))

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(0, check.evidence["fail_count"])

    def test_a_reason_without_its_blank_part_is_for_a_person(self) -> None:
        results = INTERACTIVE_052["results"].replace(" and no Blank files", "")
        check = _run(INTERACTIVE_052, methods="", results=results, table=False)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("does not give the missing blanks as a reason", _joined(check, "warns"))

    def test_the_older_count_beside_other_words_is_for_a_person(self) -> None:
        results = INTERACTIVE_050["results"].rstrip() + " The other criteria needed QC and blank injections.\n"
        check = _run(INTERACTIVE_050, methods="", results=results, table=False)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("unless the other sentences do", _joined(check, "warns"))

    def test_a_list_naming_a_criterion_twice_is_for_a_person(self) -> None:
        results = INTERACTIVE_052["results"].replace(
            "evaluated (Absolute run-order/intensity correlation)",
            "evaluated (Absolute run-order/intensity correlation; run_order_intensity_correlation)")
        check = _run(INTERACTIVE_052, results=results)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("names a criterion twice", _joined(check, "warns"))

    def test_numbers_are_read_only_as_far_as_they_are_unambiguous(self) -> None:
        cases = (
            ("a percent for a fraction", WRITER["all assessed"], "Median QC detection rate was 93.0%.", 0),
            ("an unsigned absolute value", INTERACTIVE_052, "Absolute run-order/intensity correlation was 0.4988.", 0),
            ("a thousands comma is ambiguous", INTERACTIVE_052, "Absolute run-order/intensity correlation was 5,000.", 0),
            ("a huge exponent", INTERACTIVE_052, "Absolute run-order/intensity correlation was 0e999.", 0),
            ("a wrong value", INTERACTIVE_052, "Absolute run-order/intensity correlation was 0.2.", 1),
        )
        for name, fixture, sentence, fail_count in cases:
            with self.subTest(name):
                check = _run(fixture, results=_append(fixture, sentence))
                self.assertEqual(fail_count, check.evidence["fail_count"], check.evidence["fails"])
                self.assertNotEqual(verifier.PASS, check.status)

    def test_table_numbers_are_read_only_as_far_as_they_are_unambiguous(self) -> None:
        def cell(metric, parameter, value):
            def edit(label, rows):
                if label != LABEL[metric]:
                    return rows
                return [[name, value if name == parameter else old] for name, old in rows]
            return edit

        for name, fixture, edit, verdict in (
            ("a criterion written as a percent", WRITER["all assessed"],
             cell("median_qc_detection_rate", "Criterion", ">= 80%"), verifier.PASS),
            ("an absolute criterion written plainly", INTERACTIVE_052,
             cell("run_order_intensity_correlation", "Criterion", "<= 0.3"), verifier.WARN),
            ("an unsigned absolute value", INTERACTIVE_052,
             cell("run_order_intensity_correlation", "Observed", "0.4988"), verifier.PASS),
            ("a decimal comma", INTERACTIVE_052,
             cell("run_order_intensity_correlation", "Observed", "-0,4988"), verifier.PASS),
        ):
            with self.subTest(name):
                check = _run(fixture, edit=edit)
                self.assertEqual(verdict, check.status, check.detail)

    def test_a_count_with_a_thousands_comma_is_never_a_contradiction(self) -> None:
        extra = ["Quality assurance\tObserved metric\talignment_spot_count\t18,098\t\tx"]
        check = _run(INTERACTIVE_052, extra=extra)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(0, check.evidence["fail_count"])

    def test_table_rows_qa1_sets_aside_are_named(self) -> None:
        for name, extra, message in (
            ("an observed metric the summary lacks", ["Quality assurance\tObserved metric\tqc_rsd_extra\t3\t\tx"],
             "which the QA matrix summary does not carry"),
            ("an unknown record with a value", ["Quality assurance\tMedian IS mass error\tObserved\t2.1\t\tx"],
             "which the assessment does not list"),
            ("a criterion under another section", ["Processing\tMedian QC feature RSD\tAssessment\tpass\t\tx"],
             "has rows for a QA criterion under 'processing'"),
        ):
            with self.subTest(name):
                check = _run(INTERACTIVE_052, extra=extra)
                self.assertEqual(verifier.WARN, check.status, check.detail)
                self.assertIn(message, _joined(check, "warns"))

    def test_observed_metrics_without_a_qa_matrix_are_named(self) -> None:
        check = _run(NO_QA_MATRIX, table="Section\tRecord\tParameter\tValue\tUnit or note\tSource\n"
                     "Quality assurance\tObserved metric\tsample_count\t5\t\tx\n")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("but the report carries no QA matrix summary", _joined(check, "warns"))

    def test_a_row_is_found_by_label_and_by_metric_alike(self) -> None:
        checks = _checks(INTERACTIVE_052["statuses"], INTERACTIVE_052["values"])
        extra = ["Quality assurance\tmedian_qc_rsd_percent\tAssessment\tpass\t\tx"]
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                check = _run(INTERACTIVE_052, table=_table(checks, extra=extra))
                self.assertEqual(verifier.FAIL, check.status, check.detail)

    def test_a_row_is_a_row_whatever_its_cells_hold(self) -> None:
        checks = _checks(INTERACTIVE_052["statuses"], INTERACTIVE_052["values"])
        extra = ["Quality assurance\tMedian QC feature RSD\tAssessment\tpass\tnote\u2028more\tx"]
        check = _run(INTERACTIVE_052, table=_table(checks, extra=extra))

        self.assertEqual(verifier.FAIL, check.status, check.detail)

    def test_a_bundled_table_is_compared_by_its_cells(self) -> None:
        checks = _checks(INTERACTIVE_052["statuses"], INTERACTIVE_052["values"])
        honest = _table(checks)
        shifted = honest.replace("Quality assurance\tMedian QC feature RSD\tAssessment\tnot_assessed",
                                 "Quality assurance\tMedian QC feature RSD\tAssessment pass\tnot_assessed")
        with tempfile.TemporaryDirectory() as directory:
            check = _real(Path(directory), INTERACTIVE_052).bundle({"Supplementary_Table_MS_DIAL.tsv": shifted}).qa1()

        self.assertIn("MS_DIAL_publication_reporting_bundle.zip:Supplementary_Table_MS_DIAL.tsv", check.evidence["read"])
        self.assertNotEqual(verifier.PASS, check.status)

    def test_the_record_is_checked_against_itself_and_its_summary(self) -> None:
        checks = _checks(INTERACTIVE_052["statuses"], INTERACTIVE_052["values"])
        cases = {
            "status": (dict(assessment={"status": "pass"}), "the assessment's status is 'pass'"),
            "a value the summary disagrees with": (
                dict(summary=dict(INTERACTIVE_052["summary"], run_order_intensity_correlation=-0.1)),
                "the QA matrix summary records -0.1"),
            "a value for an unassessed criterion": (
                dict(summary=dict(INTERACTIVE_052["summary"], median_qc_rsd_percent=12.0)),
                "'Median QC feature RSD' is 'not_assessed' with no value"),
        }
        for name, (options, message) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                check = Workspace(Path(directory), checks, summary=options.get("summary", INTERACTIVE_052["summary"]),
                                  results=INTERACTIVE_052["results"], assessment=options.get("assessment")).qa1()
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_a_record_with_a_repeated_key_is_not_evaluable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _real(Path(directory), INTERACTIVE_052)
            raw = (workspace.output / "MS_DIAL_publication_report.json").read_text(encoding="utf-8")
            raw = raw.replace('"qa_assessment": {', '"qa_assessment": {"status": "pass", ', 1)
            raw = raw.replace('"qa_assessment": {"status": "pass", ', '"qa_assessment": {"status": "pass", "status": "x", ', 1)
            (workspace.output / "MS_DIAL_publication_report.json").write_text(raw, encoding="utf-8")
            check = workspace.qa1()

        self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)
        self.assertIn("repeats the key", check.detail)

    def test_other_checks_do_not_pass_what_they_could_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _real(Path(directory), INTERACTIVE_052)
            workspace.bundle({"MS_DIAL_QA_Results.txt": INTERACTIVE_052["results"] * 20})
            path = workspace.output / "MS_DIAL_publication_reporting_bundle.zip"
            data = bytearray(path.read_bytes())
            data[60:90] = b"\x00" * 30
            path.write_bytes(bytes(data))
            report = verifier.verify(workspace.root, "before-publish")
            sec = [check for check in report.checks if check.check_id == "SEC-1"][0]

        self.assertEqual(verifier.WARN, sec.status, sec.detail)
        self.assertIn("could not be read", sec.detail)


QC_REASON_0 = "the run had 0 QC injection(s), and at least three are needed"
QC_REASON_2 = "the run had 2 QC injection(s), and at least three are needed"
NO_BLANK = "the run had no Blank files"
BLANK_ORDER = "no Blank file followed an injection with detected features in its batch"
QC_LABELS = ("Median QC feature RSD, Fraction of QC features with RSD <=30%, Median QC detection rate and QC PCA "
             "relative dispersion")


def _reasoned(statuses: dict, values: dict, reasons: dict) -> list[dict]:
    checks = _checks(statuses, values)
    for item in checks:
        if item["status"] == "not_assessed":
            item["reason"] = reasons[item["metric"]]
    return checks


class Interactive053Tests(unittest.TestCase):
    """From 0.5.3 each criterion that could not be assessed carries the reason it fell to."""

    OTHER_0 = (f"The other 6 ({SIX}) could not be assessed: {QC_LABELS} because {QC_REASON_0}; Fraction of features "
               f"with Sample/Blank >=3 and Median blank carryover ratio because {NO_BLANK}.")
    NONE_BLANK = {
        "statuses": {"run_order_intensity_correlation": "fail"},
        "values": {"run_order_intensity_correlation": -0.4988351017208908},
        "reasons": {metric: (NO_BLANK if metric in ("sample_blank_ratio_ge_3", "median_blank_carryover_ratio")
                             else QC_REASON_0) for metric, *_ in SEVEN[:6]},
        "summary": INTERACTIVE_052["summary"],
    }
    TWO_QC = {
        "statuses": {"sample_blank_ratio_ge_3": "pass", "run_order_intensity_correlation": "pass"},
        "values": {"sample_blank_ratio_ge_3": 0.8, "run_order_intensity_correlation": 0.1},
        "reasons": {"median_qc_rsd_percent": QC_REASON_2, "qc_features_rsd_le_30_percent": QC_REASON_2,
                    "median_qc_detection_rate": QC_REASON_2, "qc_pca_relative_dispersion": QC_REASON_2,
                    "median_blank_carryover_ratio": BLANK_ORDER},
        "summary": {"sample_count": 9, "alignment_spot_count": 90,
                    "category_counts": {"Sample": 5, "QC": 2, "Blank": 2}},
    }

    def results_0(self) -> str:
        return ("Quality assessment included 5 injections and 18098 aligned features. Of 7 prespecified QA criteria, 1 "
                "could be evaluated (Absolute run-order/intensity correlation), and 0 of them were met. " + self.OTHER_0
                + " Individual criteria and outcomes are reported in Supplementary Table S1. Criteria requiring review "
                "were: Absolute run-order/intensity correlation.\n")

    def results_2(self) -> str:
        missing = "; ".join(LABEL[m] for m in ("median_qc_rsd_percent", "qc_features_rsd_le_30_percent",
                                               "median_qc_detection_rate", "qc_pca_relative_dispersion",
                                               "median_blank_carryover_ratio"))
        return ("Quality assessment included 9 injections and 90 aligned features. Of 7 prespecified QA criteria, 2 could "
                "be evaluated (Fraction of features with Sample/Blank >=3; Absolute run-order/intensity correlation), and "
                f"2 of them were met. The other 5 ({missing}) could not be assessed: {QC_LABELS} because {QC_REASON_2}; "
                f"Median blank carryover ratio because {BLANK_ORDER}. Individual criteria and outcomes are reported in "
                "Supplementary Table S1.\n")

    def check(self, fixture: dict, results: str, *, checks=None, table=True, edit=None):
        checks = checks or _reasoned(fixture["statuses"], fixture["values"], fixture["reasons"])
        with tempfile.TemporaryDirectory() as directory:
            return Workspace(Path(directory), checks, summary=fixture["summary"], results=results,
                             table=_table(checks, edit) if table else None).qa1()

    def test_what_interactive_0_5_3_writes_passes(self) -> None:
        for name, fixture, results in (("no QC, no blanks", self.NONE_BLANK, self.results_0()),
                                       ("two QC, a blank first", self.TWO_QC, self.results_2())):
            with self.subTest(name):
                check = self.check(fixture, results)
                self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn(f"(because {QC_REASON_0}; because {NO_BLANK})", self.check(self.NONE_BLANK, self.results_0()).detail)

    def test_a_reason_the_record_does_not_give_is_refused(self) -> None:
        results_0, results_2 = self.results_0(), self.results_2()
        cases = {
            "the QC count": (self.NONE_BLANK, results_0.replace("had 0 QC", "had 2 QC"),
                             f"the assessment records {QC_REASON_0!r}"),
            "another reason": (self.TWO_QC, results_2.replace(f"Median blank carryover ratio because {BLANK_ORDER}",
                                                              f"Median blank carryover ratio because {NO_BLANK}"),
                               f"the assessment records {BLANK_ORDER!r}"),
            "a reason for a criterion that was assessed": (
                self.TWO_QC, results_2.replace("Median blank carryover ratio because",
                                               "Median blank carryover ratio and Absolute run-order/intensity correlation "
                                               "because"),
                "gives a reason 'Absolute run-order/intensity correlation' could not be assessed"),
        }
        for name, (fixture, results, message) in cases.items():
            with self.subTest(name):
                check = self.check(fixture, results)
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_where_the_record_gives_no_reasons_the_text_is_checked_against_the_counts(self) -> None:
        results_0, results_2 = self.results_0(), self.results_2()
        plain_0 = _checks(self.NONE_BLANK["statuses"], self.NONE_BLANK["values"])
        plain_2 = _checks(self.TWO_QC["statuses"], self.TWO_QC["values"])
        cases = {
            "the QC count": (self.NONE_BLANK, plain_0, results_0.replace("had 0 QC", "had 2 QC"),
                             "gives 2 QC injection(s) as the reason; the QA matrix records 0"),
            "a reason that is no reason for the criterion": (
                self.TWO_QC, plain_2, results_2.replace(f"; Median blank carryover ratio because {BLANK_ORDER}", "")
                .replace("QC PCA relative dispersion because",
                         "QC PCA relative dispersion and Median blank carryover ratio because"),
                "which is no reason for that criterion"),
            "no blanks, but there were": (
                self.TWO_QC, plain_2, results_2.replace(f"Median blank carryover ratio because {BLANK_ORDER}",
                                                        f"Median blank carryover ratio because {NO_BLANK}"),
                "gives 'no Blank files' as the reason; the QA matrix records 2"),
        }
        for name, (fixture, checks, results, message) in cases.items():
            with self.subTest(name):
                check = self.check(fixture, results, checks=checks, table=False)
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_a_reason_the_counts_do_not_decide_is_for_a_person(self) -> None:
        plain = _checks(self.NONE_BLANK["statuses"], self.NONE_BLANK["values"])
        results = self.results_0().replace(f"because {QC_REASON_0}", "because the QA matrix gives no value for it")
        check = self.check(self.NONE_BLANK, results, checks=plain, table=False)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("but the counts decide another reason (qc count)", _joined(check, "warns"))

    def test_the_records_own_reasons_are_checked(self) -> None:
        def reasons(**changes):
            return dict(self.TWO_QC["reasons"], **changes)

        cases = {
            "a reason the counts refute": (reasons(median_blank_carryover_ratio=NO_BLANK), None,
                                           "the assessment gives 'no Blank files' as the reason; the QA matrix records 2"),
            "a reason for another criterion": (reasons(median_blank_carryover_ratio=QC_REASON_2), None,
                                               "which is no reason for that criterion"),
            "the summary's reason differs": (self.TWO_QC["reasons"],
                                             {"median_blank_carryover_ratio": NO_BLANK},
                                             "its check gives"),
        }
        for name, (record_reasons, summary_reasons, message) in cases.items():
            with self.subTest(name):
                checks = _reasoned(self.TWO_QC["statuses"], self.TWO_QC["values"], record_reasons)
                fixture = dict(self.TWO_QC)
                if summary_reasons is not None:
                    fixture["summary"] = dict(self.TWO_QC["summary"], not_assessed_reasons=dict(
                        self.TWO_QC["reasons"], **summary_reasons))
                check = self.check(fixture, self.results_2(), checks=checks, table=False)
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_a_qc_criterion_needs_three_qc(self) -> None:
        statuses = dict(self.TWO_QC["statuses"], median_qc_rsd_percent="pass")
        values = dict(self.TWO_QC["values"], median_qc_rsd_percent=12.0)
        with_reasons = _reasoned(statuses, values, self.TWO_QC["reasons"])
        without = _checks(statuses, values)
        fixture = dict(self.TWO_QC, statuses=statuses, values=values)
        new = self.check(fixture, "MS-DIAL was used.", checks=with_reasons, table=False)
        old = self.check(fixture, "MS-DIAL was used.", checks=without, table=False)

        self.assertEqual(verifier.FAIL, new.status, new.detail)
        self.assertIn("a QC-based criterion needs at least three", _joined(new, "fails"))
        self.assertIn("a record from before Interactive 0.5.3", _joined(old, "warns"))
        self.assertEqual(0, old.evidence["fail_count"])

    def test_interactive_0_5_2_wording_beside_a_0_5_3_record(self) -> None:
        old_other = (f"The other 6 ({SIX}) could not be assessed because the run had 0 QC injection(s), where at least "
                     "three are needed and no Blank files.")
        check = self.check(self.NONE_BLANK, self.results_0().replace(self.OTHER_0, old_other))
        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("gives Interactive 0.5.2's reason for all", _joined(check, "warns"))

        only_qc = old_other.replace(" and no Blank files", "")
        check = self.check(self.NONE_BLANK, self.results_0().replace(self.OTHER_0, only_qc))
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn(f"the assessment records {NO_BLANK!r}", _joined(check, "fails"))

    def test_a_record_that_leaves_one_criterion_without_a_reason_is_for_a_person(self) -> None:
        checks = _reasoned(self.TWO_QC["statuses"], self.TWO_QC["values"], self.TWO_QC["reasons"])
        next(item for item in checks if item["metric"] == "median_blank_carryover_ratio").pop("reason")
        check = self.check(self.TWO_QC, self.results_2(), checks=checks, table=False)

        self.assertIn("gives no reason 'Median blank carryover ratio' could not be assessed, though it gives one",
                      _joined(check, "warns"))

    def test_table_reasons_are_read_as_written(self) -> None:
        def edit(value):
            def change(label, rows):
                return [[name, value if name == "Not assessed because" and label == LABEL["median_blank_carryover_ratio"]
                         else old] for name, old in rows]
            return change

        honest = self.check(self.TWO_QC, self.results_2(), edit=edit(BLANK_ORDER[0].upper() + BLANK_ORDER[1:] + "."))
        unknown = self.check(self.TWO_QC, self.results_2(), edit=edit("the blanks came first"))

        self.assertEqual(verifier.PASS, honest.status, honest.detail)
        self.assertEqual(verifier.WARN, unknown.status, unknown.detail)
        self.assertIn("which QA-1 cannot read as one of Interactive's reasons", _joined(unknown, "warns"))

    def test_a_reason_that_is_not_text_makes_the_record_unreadable(self) -> None:
        checks = _reasoned(self.TWO_QC["statuses"], self.TWO_QC["values"], self.TWO_QC["reasons"])
        checks[0]["reason"] = {"why": "no QC"}
        check = self.check(self.TWO_QC, self.results_2(), checks=checks, table=False)

        self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)

    def test_the_count_and_list_of_the_0_5_3_sentence_are_compared(self) -> None:
        results = self.results_2()
        for name, text, verdict, key, message in (
            ("the count", results.replace("The other 5 (", "The other 4 ("), verifier.FAIL, "fails",
             "says the other 4 could not be assessed"),
            ("the list", results.replace("QC PCA relative dispersion; Median blank", "Median blank"), verifier.FAIL, "fails",
             "as not assessed, which leaves out QC PCA relative dispersion"),
            ("a name QA-1 cannot match", results.replace("QC PCA relative dispersion; Median blank",
                                                         "QC PCA dispersion; Median blank"), verifier.WARN, "warns",
             "its list of criteria not assessed names"),
        ):
            with self.subTest(name):
                check = self.check(self.TWO_QC, text)
                self.assertEqual(verdict, check.status, check.detail)
                self.assertIn(message, _joined(check, key))

    def test_a_table_reason_the_record_does_not_carry_is_for_a_person(self) -> None:
        def row(label, rows):
            return rows + [["Not assessed because", NO_BLANK]] if label == LABEL["median_blank_carryover_ratio"] else rows

        checks = _checks(self.TWO_QC["statuses"], self.TWO_QC["values"])
        results = self.results_2().replace(f"could not be assessed: {QC_LABELS} because {QC_REASON_2}; Median blank "
                                           f"carryover ratio because {BLANK_ORDER}.",
                                           "could not be assessed because the run had 2 QC injection(s), where at "
                                           "least three are needed.")
        check = self.check(self.TWO_QC, results, checks=checks, edit=row)

        self.assertIn("a reason the assessment does not record", _joined(check, "warns"))

    def test_a_criterion_left_without_a_reason_is_for_a_person(self) -> None:
        results = self.results_2().replace(f"; Median blank carryover ratio because {BLANK_ORDER}", "")
        check = self.check(self.TWO_QC, results)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("does not say why 'Median blank carryover ratio' could not be assessed", _joined(check, "warns"))

    def test_a_reason_qa1_does_not_know_is_for_a_person(self) -> None:
        results = self.results_2().replace(BLANK_ORDER, "the blanks were run first")
        check = self.check(self.TWO_QC, results)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(0, check.evidence["fail_count"])

    def test_the_table_gives_each_reason_as_the_record_does(self) -> None:
        def row(metric, value):
            def edit(label, rows):
                return rows + [["Not assessed because", value]] if label == LABEL[metric] else rows
            return edit

        def changed(label, rows):
            return [[name, NO_BLANK if name == "Not assessed because" and label == LABEL["median_blank_carryover_ratio"]
                     else value] for name, value in rows]

        for name, edit, message in (
            ("a reason the record does not give", changed, "the assessment records 'no Blank file followed"),
            ("a reason for a criterion that was assessed", row("run_order_intensity_correlation", NO_BLANK),
             "as why 'Absolute run-order/intensity correlation' could not be assessed; the assessment passed it"),
        ):
            with self.subTest(name):
                check = self.check(self.TWO_QC, self.results_2(), edit=edit)
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_a_record_giving_a_reason_for_an_assessed_criterion_contradicts_itself(self) -> None:
        checks = _reasoned(self.TWO_QC["statuses"], self.TWO_QC["values"], self.TWO_QC["reasons"])
        next(item for item in checks if item["metric"] == "sample_blank_ratio_ge_3")["reason"] = NO_BLANK
        check = self.check(self.TWO_QC, self.results_2(), checks=checks)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("gives a reason it could not be assessed", _joined(check, "fails"))

    def test_the_shared_sentence_is_read_by_the_version_of_the_record(self) -> None:
        # "because the run had no Blank files" is 0.5.2's reason for the run and 0.5.3's for each
        # criterion; a QC criterion given it is a contradiction only in a record that carries reasons.
        statuses = {metric: "pass" for metric, *_ in SEVEN} | {"median_qc_detection_rate": "not_assessed"}
        values = {metric: 0.9 for metric, *_ in SEVEN}
        values.pop("median_qc_detection_rate")
        summary = {"sample_count": 9, "alignment_spot_count": 90, "category_counts": {"Sample": 6, "QC": 3, "Blank": 0}}
        results = ("Of 7 prespecified QA criteria, 6 could be evaluated (" + "; ".join(
            LABEL[m] for m, *_ in SEVEN if m != "median_qc_detection_rate") + "), and 6 of them were met. The other 1 "
            f"(Median QC detection rate) could not be assessed because {NO_BLANK}.")
        fixture = {"statuses": statuses, "values": values, "summary": summary,
                   "reasons": {"median_qc_detection_rate": "the QA matrix gives no value for it"}}
        old = self.check(fixture, results, checks=_checks(statuses, values), table=False)
        new = self.check(fixture, results, table=False)

        self.assertEqual(0, old.evidence["fail_count"], old.evidence["fails"])
        self.assertEqual(verifier.FAIL, new.status, new.detail)


class RecordReadingTests(unittest.TestCase):
    """What QA-1 notes, or refuses, about the record itself, one branch each."""

    TWO_QC = Interactive053Tests.TWO_QC

    def check(self, checks, summary, results, assessment=None):
        with tempfile.TemporaryDirectory() as directory:
            return Workspace(Path(directory), checks, summary=summary, results=results, assessment=assessment).qa1()

    def reasoned(self, **changes):
        return _reasoned(self.TWO_QC["statuses"], self.TWO_QC["values"], dict(self.TWO_QC["reasons"], **changes))

    def test_what_the_record_cannot_give_is_noted(self) -> None:
        results = Interactive053Tests().results_2()
        summary = self.TWO_QC["summary"]
        cases = {
            "a reason with no count to compare": (
                self.reasoned(), {"sample_count": 9, "alignment_spot_count": 90}, None,
                "which the report has no figure to compare with"),
            "counts that are not an object": (self.reasoned(), dict(summary, category_counts=[5, 2, 2]), None,
                                              "category_counts is not an object"),
            "a count that is not a count": (self.reasoned(), dict(summary, sample_count="nine"), None,
                                            "qa_report.summary.sample_count is 'nine', not a count"),
            "evaluated that is not a count": (self.reasoned(), summary, {"evaluated": "two"},
                                              "qa_assessment.evaluated is 'two', not a count"),
            "reasons that are not an object": (self.reasoned(), dict(summary, not_assessed_reasons=["x"]), None,
                                               "not_assessed_reasons is not an object"),
            "no summary beside evaluated criteria": (self.reasoned(), None, None,
                                                     "the report carries no QA matrix summary, although 2 criteria"),
        }
        for name, (checks, summary_value, assessment, message) in cases.items():
            with self.subTest(name):
                check = self.check(checks, summary_value, results, assessment)
                self.assertIn(message, _joined(check, "warns"))

    def test_what_the_record_contradicts_is_refused(self) -> None:
        results = Interactive053Tests().results_2()
        summary = self.TWO_QC["summary"]
        cases = {
            "no matrix, but there is one": (self.reasoned(median_blank_carryover_ratio="no LC-MS QA matrix was supplied"),
                                            summary, "gives 'no LC-MS QA matrix was supplied' as a reason"),
            "a summary reason for an assessed criterion": (
                self.reasoned(), dict(summary, not_assessed_reasons=dict(self.TWO_QC["reasons"],
                                                                          run_order_intensity_correlation=NO_BLANK)),
                "the QA matrix summary gives a reason 'Absolute run-order/intensity correlation' could not be assessed"),
            "an assessed criterion with no value in the summary": (
                self.reasoned(), dict(summary, run_order_intensity_correlation=None),
                "'Absolute run-order/intensity correlation' is 'pass', but the QA matrix summary records no value"),
        }
        for name, (checks, summary_value, message) in cases.items():
            with self.subTest(name):
                check = self.check(checks, summary_value, results)
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(message, _joined(check, "fails"))

    def test_groups_that_name_a_criterion_qa1_cannot_match_or_twice(self) -> None:
        results = Interactive053Tests().results_2()
        cases = {
            "an unknown name": (results.replace(f"Median blank carryover ratio because {BLANK_ORDER}",
                                                f"Median blank carry-over ratio because {BLANK_ORDER}"),
                                "gives a reason for median blank carry-over ratio, which QA-1 cannot match"),
            "one criterion given two reasons": (
                results.replace("QC PCA relative dispersion because",
                                "QC PCA relative dispersion and Median blank carryover ratio because"),
                "gives 'Median blank carryover ratio' more than one reason"),
        }
        for name, (text_value, message) in cases.items():
            with self.subTest(name):
                check = self.check(self.reasoned(), self.TWO_QC["summary"], text_value)
                self.assertNotEqual(verifier.PASS, check.status)
                self.assertIn(message, _joined(check, "warns"))


class CommandLineTests(unittest.TestCase):
    def test_output_survives_characters_a_console_cannot_print(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _real(Path(directory), INTERACTIVE_052,
                              results=_append(INTERACTIVE_052, "Median QC feature RSD ≤ 30% � was fine."))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                verifier.main([str(workspace.root), "--stage", "before-publish", "--json"])
            printed = buffer.getvalue()
            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                verifier.main([str(workspace.root), "--stage", "before-publish"])

        printed.encode("ascii")
        checks = {check["check_id"]: check for check in json.loads(printed)["checks"]}
        self.assertIn("≤", json.dumps(checks["QA-1"], ensure_ascii=False))
        self.assertIn("QA-1", text.getvalue())


if __name__ == "__main__":
    unittest.main()
