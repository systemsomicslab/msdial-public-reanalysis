"""Polarity-split units (plan --polarity-split, the author's decision of 2026-11-19).

"Polarity switching units are analysed: for each file run a POS process and a NEG process separately." These tests
hold: a unit held only on polarity_switching becomes two run units with derived curated selections the validator
passes in place of the parent; a unit held on anything else is not split; the option combines with
--download-only-held; the validator refuses a child of the wrong shape or one that is not the derivation of its parent;
the approval records one polarity_split_planned per child; the runner never compares the two children in the
cross-unit check, and their shared raw data stay until both have ended.

Only public accession and analysis names (the eval fixtures' ST002747) and synthetic names are used. Nothing is
downloaded and no Console runs.
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402,F401  (puts scripts/ on the path)
import decision_records  # noqa: E402
import test_campaign_contract as contract  # noqa: E402
import test_campaign_curated_selection as cs  # noqa: E402
import triage_eval as te  # noqa: E402
from campaign import curated, plan, policy, ports  # noqa: E402

SPLIT_UNIT = "AN004456"


def switching_docs(unit: str = SPLIT_UNIT, *, question: str = "polarity_switching") -> dict:
    """The eval accession's triage as v2 (hand-written, as skill 1.1.0 wrote it), with `unit` held on polarity
    switching: every row it kept is left out as polarity_switching, its ion mode switching, one question for a person.
    write_triage declares the unit Both in the Catalog evidence the agent read."""
    docs = te.to_v2(te.build_good(cs.ACCESSION))
    doc = docs[unit]
    moved = [{"row_index": row["row_index"], "sample_id": row["sample_id"], "raw_file": row["raw_file"],
              "member_path": row["member_path"], "reason": "polarity_switching", "belongs_to_unit": None,
              "detail": "The study declares positive/negative polarity switching within each run.",
              "confidence": "high", "rationale": "Every run of the analysis is declared polarity switching."}
             for row in doc["rows"]]
    doc["excluded_rows"] = sorted(doc["excluded_rows"] + moved, key=lambda item: item["row_index"])
    doc["rows"] = []
    doc["unit_technical"]["ion_mode"] = "switching"
    doc["status"] = "needs_person"
    doc["needs_person"] = [{"code": question, "question": "Run the switching files once per polarity?",
                            "row_indices": [item["row_index"] for item in moved], "member_paths": []}]
    doc["flags"] = [{"code": "polarity_switching", "certainty": "confirmed", "detail": "declared switching"}]
    if question not in ("polarity_switching", "other"):
        doc["flags"].append({"code": question, "certainty": "suspected", "detail": "another question"})
    return docs


def code_root(root: Path) -> "decision_records.Code":
    """The released code a hand-written v2 payload's record names (skill 1.1.0, which wrote payloads by hand)."""
    return decision_records.Code.from_root(root / "code", skill_version=decision_records.HAND_WRITTEN_SKILL_VERSION)


class _WithCode:
    """Every validator run reads the records' code from code_root(), as a release bundle's replay does."""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)  # type: ignore[attr-defined]
        self.code = code_root(root)
        original = curated.run_validator
        patcher = mock.patch.object(curated, "run_validator",
                                    lambda folder, **options: original(folder, code_root=self.code.root, **options))
        patcher.start()
        self.addCleanup(patcher.stop)  # type: ignore[attr-defined]


def write_triage(root: Path, docs: dict, code: "decision_records.Code") -> Path:
    """cs.write_triage, with the decision record a v2 payload needs beside each payload (decision_records)."""
    triage_root = cs.write_triage(root, docs)
    folder = triage_root / cs.ACCESSION
    catalog = json.loads((folder / "inputs" / "catalog.json").read_text(encoding="utf-8"))
    for unit in catalog["units"]:
        if unit["unit_id"] == SPLIT_UNIT:
            unit["declared"]["ion_mode"] = "Both"
    (folder / "inputs" / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    for unit in docs:
        decision_records.write_record(folder / f"{unit}.json", code,
                                      inputs=[{"role": "archive_listing", "path": "inputs/archive-members.tsv"}])
    return triage_root


@unittest.skipUnless(contract.AVAILABLE, "the Interactive and Catalog checkouts are not where this test looks")
class PlanTests(_WithCode, cs._Root):
    def setUp(self) -> None:
        super().setUp()
        self.database = self.root / "catalog" / "catalog.sqlite"
        self.database.parent.mkdir()
        cs.build_catalog(self.database)
        self.campaign_dir = self.root / "campaign"

    def manifest(self, triage_dir: Path, *, held: bool = False, split: bool = True) -> dict:
        catalog = ports.read_only_catalog(self.database)
        try:
            return plan.build_manifest(
                catalog, pool="declared", campaign_id="split", analysis_purpose="annotation",
                workspace_root=self.workspace_root, raw_retention_policy="delete_after_validated_output",
                pins={"catalog": {"version": "0.6.1"}, "libraries": [], "interactive": {"version": "0.5.69"}},
                profile=None, campaign_policy=policy.CampaignPolicy(),
                class_decision=lambda unit_id: ports.decide_class(catalog, unit_id, "annotation"),
                catalog_database=str(self.database), triage_dir=triage_dir, download_only_held=held,
                polarity_split_dir=self.campaign_dir if split else None,
            )
        finally:
            catalog.close()

    def test_a_unit_held_on_polarity_switching_alone_becomes_two_run_units(self) -> None:
        triage_root = write_triage(self.root, switching_docs(), self.code)
        manifest = self.manifest(triage_root)
        units = {unit.get("unit_key", unit["unit_id"]): unit for unit in manifest["units"]}
        self.assertIn(f"{SPLIT_UNIT}-pos", units, manifest["exclusions"])
        self.assertIn(f"{SPLIT_UNIT}-neg", units)
        self.assertNotIn(SPLIT_UNIT, units)
        pos, neg = units[f"{SPLIT_UNIT}-pos"], units[f"{SPLIT_UNIT}-neg"]
        self.assertEqual((pos["unit_id"], neg["unit_id"]), (SPLIT_UNIT, SPLIT_UNIT), "the same Catalog unit")
        self.assertEqual((pos["workspace_name"], neg["workspace_name"]), (f"{SPLIT_UNIT}-pos", f"{SPLIT_UNIT}-neg"))
        self.assertEqual((pos["ion_mode"], neg["ion_mode"]), ("Positive", "Negative"))
        self.assertEqual(pos["group_id"], neg["group_id"])
        self.assertEqual(neg["order_index"], pos["order_index"] + 1, "the children run one after the other")
        self.assertEqual(pos["approved_class_digest"], neg["approved_class_digest"])
        self.assertEqual(pos["polarity_split"], {"parent_unit_id": SPLIT_UNIT, "ion_mode": "positive",
                                                 "sibling_unit_key": f"{SPLIT_UNIT}-neg"})
        parent = triage_root / cs.ACCESSION / f"{SPLIT_UNIT}.json"
        parent_sha = curated.sha256_file(parent)
        for unit, mode, other in ((pos, "positive", "negative"), (neg, "negative", "positive")):
            chosen = unit["curated_selection"]
            self.assertEqual(chosen["polarity_split"], {"ion_mode": mode, "reason": "polarity_switching",
                                                        "parent_unit_id": SPLIT_UNIT, "sibling_ion_mode": other})
            self.assertEqual(chosen["validator"]["verdict"], "pass")
            self.assertEqual(chosen["validator"]["scope"], "polarity_split_unit")
            self.assertEqual(chosen["derived_from"]["triage_sha256"], parent_sha)
            self.assertEqual(Path(chosen["derived_from"]["triage_path"]), parent.resolve())
            self.assertEqual(chosen["derived_from"]["decision"]["date"], "2026-11-19")
            self.assertIsNone(chosen["triage_record_sha256"], "no decision record sits beside a child")
            self.assertEqual(chosen["rows_kept"], 13)
            child_path = Path(chosen["triage_path"])
            self.assertEqual(child_path, (self.campaign_dir / "handoffs" / unit["unit_key"]
                                          / f"{SPLIT_UNIT}.polarity-split.json").resolve())
            self.assertEqual(curated.sha256_file(child_path), chosen["triage_sha256"])
            child = json.loads(child_path.read_text(encoding="utf-8"))
            self.assertEqual(child["status"], "ready")
            self.assertEqual(child["needs_person"], [])
            self.assertEqual(child["unit_technical"]["ion_mode"], mode)
            self.assertEqual(child["polarity_split"], chosen["polarity_split"])
            self.assertEqual({row["ion_mode"] for row in child["rows"]}, {"switching"})
            self.assertFalse([row for row in child["excluded_rows"] if row["reason"] == "polarity_switching"])
        self.assertEqual(pos["curated_selection"]["selection_sha256"], neg["curated_selection"]["selection_sha256"],
                         "both children select the same files")
        totals = manifest["totals"]
        self.assertEqual(totals["polarity_split"]["parents"], 1)
        self.assertEqual(totals["polarity_split"]["children"], 2)
        self.assertEqual(totals["polarity_split"]["parent_known_bytes"], pos["known_bytes"])
        others = sum(unit["known_bytes"] for key, unit in units.items() if not unit.get("polarity_split"))
        self.assertEqual(totals["per_unit_known_bytes"], others + pos["known_bytes"], "a parent's bytes counted once")
        self.assertTrue(manifest["triage"]["polarity_split"][SPLIT_UNIT]["planned"])
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("POLARITY-SPLIT 1 unit(s) into 2 run units", text)
        self.assertIn(f"{SPLIT_UNIT} -> {SPLIT_UNIT}-neg, {SPLIT_UNIT}-pos", text)
        events = plan.decision_events(manifest, "sha256:" + "0" * 64, {"approval_id": "A", "covers": ["1", "3", "4", "5"]})
        planned = {key: detail for kind, detail, key in events if kind == "polarity_split_planned"}
        self.assertEqual(sorted(planned), [f"{SPLIT_UNIT}-neg", f"{SPLIT_UNIT}-pos"])
        self.assertEqual(planned[f"{SPLIT_UNIT}-neg"]["parent_unit_id"], SPLIT_UNIT)
        self.assertEqual(planned[f"{SPLIT_UNIT}-neg"]["sibling_unit_key"], f"{SPLIT_UNIT}-pos")
        self.assertEqual(planned[f"{SPLIT_UNIT}-neg"]["parent_sha256"], parent_sha)
        approved = [detail for kind, detail, _key in events if kind == "manifest_approved"][0]
        self.assertEqual(approved["polarity_split"], [f"{SPLIT_UNIT}-neg", f"{SPLIT_UNIT}-pos"])
        recorded = {key: detail for kind, detail, key in events if kind == "triage_recorded"}
        self.assertEqual(recorded[f"{SPLIT_UNIT}-pos"]["derived_from"]["triage_relative_path"], f"{cs.ACCESSION}/{SPLIT_UNIT}.json")
        self.assertEqual(recorded[f"{SPLIT_UNIT}-pos"]["derived_from"]["triage_sha256"], parent_sha)
        self.assertEqual(recorded[f"{SPLIT_UNIT}-pos"]["triage_relative_path"],
                         f"handoffs/{SPLIT_UNIT}-pos/{SPLIT_UNIT}.polarity-split.json")
        self.assertFalse(any(":\\" in json.dumps(detail) for _kind, detail, _key in events
                             if _kind in ("triage_recorded", "polarity_split_planned")), "ledger lines hold no absolute path")
        # A plan made again writes the same children.
        again = self.manifest(triage_root)
        self.assertEqual([unit["curated_selection"]["triage_sha256"] for unit in again["units"] if unit.get("polarity_split")],
                         [pos["curated_selection"]["triage_sha256"], neg["curated_selection"]["triage_sha256"]])

    def test_without_the_option_the_unit_is_held(self) -> None:
        manifest = self.manifest(write_triage(self.root, switching_docs(), self.code), split=False)
        self.assertFalse([unit for unit in manifest["units"] if unit.get("polarity_split")])
        self.assertNotIn("polarity_split", manifest["totals"])
        self.assertFalse((self.campaign_dir / "handoffs").exists())

    def test_a_unit_held_on_another_question_is_not_split(self) -> None:
        manifest = self.manifest(write_triage(self.root, switching_docs(question="mixed_acquisition"), self.code))
        self.assertFalse([unit for unit in manifest["units"] if unit.get("polarity_split")])
        self.assertIn(SPLIT_UNIT, {item["unit_id"] for item in manifest["exclusions"]})
        self.assertFalse((self.campaign_dir / "handoffs").exists())

    def test_with_download_only_held_the_split_unit_runs_and_the_rest_is_download_only(self) -> None:
        docs = switching_docs()
        docs["AN004457"]["status"] = "needs_person"
        docs["AN004457"]["needs_person"] = [{"code": "other", "question": "Which study is this?"}]
        manifest = self.manifest(write_triage(self.root, docs, self.code), held=True)
        units = {unit.get("unit_key", unit["unit_id"]): unit for unit in manifest["units"]}
        for key in (f"{SPLIT_UNIT}-pos", f"{SPLIT_UNIT}-neg"):
            self.assertNotIn("download_only", units[key], "a polarity-split child is a run unit")
            self.assertIn("curated_selection", units[key])
        self.assertNotIn(SPLIT_UNIT, units)
        held = {key for key, unit in units.items() if unit.get("download_only")}
        self.assertEqual(held, {"AN004454", "AN004455", "AN004457"}, "the accession holds for AN004457")
        self.assertEqual(manifest["totals"]["run_units"], 2)
        self.assertEqual(manifest["totals"]["download_only"]["units"], 3)

    def test_a_child_that_does_not_pass_falls_back(self) -> None:
        docs = switching_docs()
        # A kept row of the switching unit names no member: the validator errs on the children.
        docs[SPLIT_UNIT]["excluded_rows"][0]["member_path"] = None
        manifest = self.manifest(write_triage(self.root, docs, self.code), held=True)
        units = {unit.get("unit_key", unit["unit_id"]): unit for unit in manifest["units"]}
        self.assertIn(SPLIT_UNIT, units)
        self.assertIn("download_only", units[SPLIT_UNIT])
        self.assertFalse([unit for unit in manifest["units"] if unit.get("polarity_split")])

    def test_the_option_needs_the_triage(self) -> None:
        catalog = ports.read_only_catalog(self.database)
        try:
            with self.assertRaisesRegex(plan.PlanError, "--triage-dir"):
                plan.build_manifest(
                    catalog, pool="declared", campaign_id="split", analysis_purpose="annotation",
                    workspace_root=self.workspace_root, raw_retention_policy="keep",
                    pins={"catalog": {"version": "0.6.1"}, "libraries": []}, profile=None,
                    campaign_policy=policy.CampaignPolicy(), class_decision=lambda unit_id: {},
                    catalog_database=str(self.database), polarity_split_dir=self.campaign_dir)
        finally:
            catalog.close()


class ValidatorTests(_WithCode, unittest.TestCase):
    """The validator's polarity-split checks, on the payloads alone and beside a parent's accession."""

    def setUp(self) -> None:
        super().setUp()
        self.module = curated.validator_module()
        self.parent = switching_docs()[SPLIT_UNIT]
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def child(self, mode: str = "positive") -> dict:
        return self.module.derive_polarity_split(self.parent, mode, triage_relative_path=f"{cs.ACCESSION}/{SPLIT_UNIT}.json",
                                                 triage_sha256="a" * 64, triage_record_sha256=None)

    def test_the_rule_reaches_only_a_unit_held_on_polarity_switching_alone(self) -> None:
        self.assertEqual(self.module.polarity_split_refusal(self.parent), "")
        for question in ("mixed_acquisition", "other"):
            self.assertIn("not polarity_switching alone",
                          self.module.polarity_split_refusal(switching_docs(question=question)[SPLIT_UNIT]))
        both = copy.deepcopy(self.parent)
        both["needs_person"].append({"code": "shared_content", "question": "?"})
        self.assertTrue(self.module.polarity_split_refusal(both))
        positive = copy.deepcopy(self.parent)
        positive["unit_technical"]["ion_mode"] = "positive"
        self.assertIn("not switching", self.module.polarity_split_refusal(positive))
        blocked = copy.deepcopy(self.parent)
        blocked["flags"].append({"code": "mixed_acquisition", "certainty": "suspected", "detail": "x"})
        self.assertIn("other blocking flags: mixed_acquisition", self.module.polarity_split_refusal(blocked))
        unresolved = copy.deepcopy(self.parent)
        unresolved["excluded_rows"][0]["member_path"] = None
        self.assertIn("name no archive member", self.module.polarity_split_refusal(unresolved))
        self.assertIn("not split again", self.module.polarity_split_refusal(self.child()))

    def test_the_child_shape(self) -> None:
        child = self.child("negative")
        self.assertEqual(self.module.polarity_split_shape_problems(child), [])
        self.assertEqual(child["polarity_split"], {"ion_mode": "negative", "reason": "polarity_switching",
                                                   "parent_unit_id": SPLIT_UNIT, "sibling_ion_mode": "positive"})
        schema = self.module.load_schema(self.module.SCHEMA_V2_ID)
        self.assertEqual(self.module.schema_errors(child, schema, schema), [])
        cases = {
            "sibling_ion_mode": ("positive", "negative", "sibling"),
            "reason": ("polarity_switching", "other", "reason"),
            "parent_unit_id": (SPLIT_UNIT, "AN000000", "parent_unit_id"),
            "ion_mode": ("negative", "both", "positive or negative"),
        }
        for key, (_good, bad, said) in cases.items():
            broken = copy.deepcopy(child)
            broken["polarity_split"][key] = bad
            problems = self.module.polarity_split_shape_problems(broken)
            self.assertTrue(any(said in problem for problem in problems), (key, problems))
        broken = copy.deepcopy(child)
        broken["polarity_split"]["extra"] = 1
        self.assertTrue(self.module.polarity_split_shape_problems(broken))
        self.assertTrue(self.module.schema_errors(broken, schema, schema))
        broken = copy.deepcopy(child)
        broken["unit_technical"]["ion_mode"] = "positive"
        self.assertTrue(any("unit_technical.ion_mode" in p for p in self.module.polarity_split_shape_problems(broken)))
        broken = copy.deepcopy(child)
        broken["status"] = "needs_person"
        self.assertTrue(self.module.polarity_split_shape_problems(broken))
        broken = copy.deepcopy(child)
        del broken["derived_from"]
        self.assertTrue(any("derived_from" in p for p in self.module.polarity_split_shape_problems(broken)))

    def _validate(self, child: dict, *, in_directory: bool = False) -> dict:
        triage_root = write_triage(self.root, switching_docs(), self.code)
        folder = triage_root / cs.ACCESSION
        # In the directory, the child takes its parent's place as the unit's triage file.
        path = folder / f"{SPLIT_UNIT}.json" if in_directory else self.root / f"{SPLIT_UNIT}.polarity-split.json"
        path.write_text(json.dumps(child), encoding="utf-8")
        return curated.run_validator(folder, polarity_split=[] if in_directory else [path])

    def _parent_sha(self) -> tuple[str, str]:
        triage_root = write_triage(self.root, switching_docs(), self.code)
        folder = triage_root / cs.ACCESSION
        return (curated.sha256_file(folder / f"{SPLIT_UNIT}.json"), curated.sha256_file(folder / f"{SPLIT_UNIT}.record.json"))

    def test_a_child_derived_from_its_parent_passes_for_its_unit(self) -> None:
        sha, record = self._parent_sha()
        for mode in ("positive", "negative"):
            child = self.module.derive_polarity_split(self.parent, mode, triage_relative_path=f"{cs.ACCESSION}/{SPLIT_UNIT}.json",
                                                      triage_sha256=sha, triage_record_sha256=record)
            validation = self._validate(child)
            self.assertEqual(curated.polarity_split_verdict(validation, SPLIT_UNIT), ("pass", ""),
                             curated.finding_lines(validation["report"], 10))
            self.assertEqual(validation["report"]["units"][SPLIT_UNIT]["status"], "ready")

    def test_a_child_changed_after_its_derivation_is_refused(self) -> None:
        sha, record = self._parent_sha()
        child = self.module.derive_polarity_split(self.parent, "positive", triage_relative_path=f"{cs.ACCESSION}/{SPLIT_UNIT}.json",
                                                  triage_sha256=sha, triage_record_sha256=record)
        child["rows"] = child["rows"][1:]
        validation = self._validate(child)
        verdict, said = curated.polarity_split_verdict(validation, SPLIT_UNIT)
        self.assertEqual(verdict, "error")
        self.assertIn("T-SPLIT", said)
        self.assertIn("differs in rows", said)

    def test_a_child_of_another_parent_is_refused(self) -> None:
        self._parent_sha()
        child = self.child()  # names a parent sha256 the directory's payload does not have
        verdict, said = curated.polarity_split_verdict(self._validate(child), SPLIT_UNIT)
        self.assertEqual(verdict, "error")
        self.assertIn("derived_from", said)

    def test_a_child_in_the_triage_directory_is_refused(self) -> None:
        validation = self._validate(self.child(), in_directory=True)
        self.assertEqual(validation["verdict"], "error")
        self.assertTrue(any(item["check"] == "T-SPLIT" for item in validation["report"]["findings"]))



def split_world(root: Path, units: dict, groups: dict) -> "fakes.World":
    """cs.curated_world, with the units named <parent>-pos / <parent>-neg planned as polarity-split children (their
    manifest entries carry polarity_split, as plan._curated_units writes it). Both children share the parent's triage
    here, so their inputs are identical, as the derived children's are."""
    original = fakes.World.__init__

    def init(self, world_root, unit_ids, **options):
        for unit in (options.get("manifest") or {}).get("units") or []:
            key = unit["unit_key"]
            if key.endswith(("-pos", "-neg")):
                mode = "positive" if key.endswith("-pos") else "negative"
                unit["polarity_split"] = {"parent_unit_id": unit["unit_id"], "ion_mode": mode,
                                          "sibling_unit_key": unit["unit_id"] + ("-neg" if mode == "positive" else "-pos")}
        original(self, world_root, unit_ids, **options)

    with mock.patch.object(fakes.World, "__init__", init):
        return cs.curated_world(root, units, groups=groups)


class RunnerTests(cs._Root):
    CHILDREN = {"P-pos": {"catalog": "P", "workspace": "P-pos"}, "P-neg": {"catalog": "P", "workspace": "P-neg"}}

    def calls(self, world, name: str) -> list[dict]:
        return [call for kind, call in world.interactive.calls if kind == name]

    def test_the_children_run_one_after_the_other_and_are_never_compared_with_each_other(self) -> None:
        world = split_world(self.root, dict(self.CHILDREN), {"g": ["P-pos", "P-neg"]})
        world.run()
        self.assertEqual([cs.unit_row(world, key)["state"] for key in ("P-pos", "P-neg")], ["done", "done"])
        self.assertEqual(world.interactive.cross_unit_calls, [], "rule D would refuse the second child")
        verdicts = [item["detail"] for item in cs.events(world, "cross_unit_verdict")]
        self.assertEqual(sorted((tuple(item["units"]), item["verdict"], item["reason"]) for item in verdicts),
                         [(("P-neg",), "not_applicable", "polarity_split_sibling"),
                          (("P-pos",), "not_applicable", "polarity_split_sibling")])
        starts = [unit for unit, kind in world.interactive.console_starts]
        self.assertEqual(sorted(set(starts)), ["P-neg", "P-pos"])
        first, second = starts[0], ({"P-neg", "P-pos"} - {starts[0]}).pop()
        cut = starts.index(second)
        self.assertEqual(set(starts[:cut]), {first})
        self.assertEqual(set(starts[cut:]), {second}, "one child's Consoles, then the other's, never interleaved")

    def test_the_shared_raw_data_go_only_after_both_children_have_downloaded_and_each_has_ended(self) -> None:
        world = split_world(self.root, dict(self.CHILDREN), {"g": ["P-pos", "P-neg"]})
        world.gate.exits.update(pre_cleanup=0, final=0)
        world.run()
        book_rows = {key: cs.unit_row(world, key) for key in ("P-pos", "P-neg")}
        self.assertEqual({row["raw_disposition"] for row in book_rows.values()}, {"released"})
        with world.open() as book:
            waits = [json.loads(row["detail_json"]) for row in book.transitions("P-pos") if row["to_state"] == "waiting_retry"]
        self.assertTrue(any(item.get("waiting_for_group_preflight") == ["P-neg"] for item in waits),
                        "the first child waits for its sibling's download before it runs")
        order = [(kind, call) for kind, call in world.interactive.calls if kind in ("download", "cleanup", "discard")]
        downloads = [index for index, (kind, call) in enumerate(order) if kind == "download"]
        deletions = [index for index, (kind, call) in enumerate(order) if kind in ("cleanup", "discard")]
        self.assertEqual(len(downloads), 2)
        self.assertGreater(min(deletions), max(downloads), "no child's raw data go before both children downloaded")
        self.assertEqual(len(world.interactive.fetches), 1, "the shared object is fetched once")
        self.assertEqual(len(deletions), 2, "each child deletes its own raw tree after its own end")

    def test_each_child_is_still_compared_with_the_groups_other_units(self) -> None:
        units = {**self.CHILDREN, "Q": {}}
        world = split_world(self.root, units, {"g": ["P-pos", "P-neg", "Q"]})
        world.run()
        sent = [sorted(Path(path).parent.parent.name for path in call) for call in world.interactive.cross_unit_calls]
        self.assertTrue(sent, "the check runs for the group")
        for members in sent:
            self.assertFalse({"P-pos", "P-neg"} <= set(members), f"the two children compared with each other: {members}")
        self.assertEqual([cs.unit_row(world, key)["state"] for key in ("P-pos", "P-neg", "Q")], ["done"] * 3)

    def test_each_child_claims_and_releases_under_its_own_run_key(self) -> None:
        """Interactive 0.5.70 keys a child <unit_id>-pos / -neg in the store, so a sibling's release never takes its
        claims."""
        world = split_world(self.root, dict(self.CHILDREN), {"g": ["P-pos", "P-neg"]})
        # The Catalog lists the objects under the Catalog unit id both children share.
        world.unit_objects["P"] = list(world.unit_objects["P-pos"])
        world.scripts["P-neg"] = fakes.UnitScript(disposition="exclude")
        world.run()
        claimed = [unit for call in self.calls(world, "pre_claim") for unit in call["units"]]
        self.assertTrue(claimed)
        self.assertTrue(set(claimed) <= {"P-pos", "P-neg"}, claimed)
        released = [call["unit_id"] for call in self.calls(world, "release_claims")]
        self.assertTrue(set(released) <= {"P-pos", "P-neg"}, released)
        self.assertNotIn("P", claimed + released, "never under the shared Catalog unit id")

    def test_a_child_excluded_at_its_preflight_leaves_its_sibling_running(self) -> None:
        """A child whose files hold no scan of its polarity is excluded at its preflight
        (polarity_split_no_scans_of_child_polarity); its sibling still runs, and only the excluded child's own tree goes."""
        world = split_world(self.root, dict(self.CHILDREN), {"g": ["P-pos", "P-neg"]})
        world.scripts["P-neg"] = fakes.UnitScript(disposition="exclude")
        world.gate.exits.update(pre_cleanup=0, final=0)
        world.run()
        pos, neg = cs.unit_row(world, "P-pos"), cs.unit_row(world, "P-neg")
        self.assertEqual((pos["state"], neg["state"]), ("done", "excluded"))
        self.assertEqual({unit for unit, _kind in world.interactive.console_starts}, {"P-pos"})
        order = [(kind, call) for kind, call in world.interactive.calls if kind in ("download", "cleanup", "discard")]
        downloads = [index for index, (kind, _call) in enumerate(order) if kind == "download"]
        deletions = [(index, call.get("manifest_path")) for index, (kind, call) in enumerate(order) if kind in ("cleanup", "discard")]
        self.assertEqual(len(downloads), 2)
        self.assertGreater(min(index for index, _path in deletions), max(downloads))
        self.assertEqual(sorted(path for _index, path in deletions), sorted([pos["manifest_path"], neg["manifest_path"]]),
                         "each child deletes only its own raw tree")
        self.assertEqual(len(world.interactive.fetches), 1, "the shared object is fetched once")


if __name__ == "__main__":
    unittest.main()


class ApprovalReviewLogOfChildrenTests(unittest.TestCase):
    """The approval's digest_approval entry for a polarity-split or own-polarity child goes to the parent triage's
    review log (derived_from), not to a path under the triage folder named after the child's copy in handoffs."""

    def test_a_childs_approval_is_logged_once_in_the_parents_review_log(self) -> None:
        import importlib.util, tempfile
        from pathlib import Path as _Path
        root = _Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("campaign_runner_for_log", root / "scripts" / "campaign-runner.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as tmp:
            triage = _Path(tmp) / "triage"
            (triage / "ST000001").mkdir(parents=True)
            parent_sha = "c" * 64
            child = lambda pol, sha: {"triage_relative_path": f"handoffs/u1-{pol}/u1.polarity-split.json", "triage_sha256": sha,
                                      "derived_from": {"kind": "polarity_split", "triage_relative_path": "ST000001/u1.json",
                                                       "triage_sha256": parent_sha}}
            manifest = {"triage": {"directory": str(triage)},
                        "units": [{"curated_selection": child("pos", "a" * 64)}, {"curated_selection": child("neg", "b" * 64)}]}
            approval = {"approved_at": "2026-11-20T00:00:00+00:00", "approval_id": "x", "approved_by": "p"}
            runner._log_digest_approval(manifest, "sha256:" + "d" * 64, approval)
            log = triage / "ST000001" / "review-log.jsonl"
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn(parent_sha, lines[0])
            self.assertFalse((triage / "handoffs").exists())
