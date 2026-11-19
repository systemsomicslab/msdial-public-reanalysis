"""Partner units run at their own polarities (plan --polarity-split, the author's decision of 2026-11-20).

A repository may register a positive and a negative analysis over one set of polarity-switching files: two Catalog
units, two unit ids, the same files (Metabolomics Workbench ST003452, ST004572). Such a unit is not split in two: it
runs once at its own polarity, as <unit_id>-pos or <unit_id>-neg, and names the other unit as its partner. These tests
hold: the rule reaches a held unit of positive or negative unit ion mode, and a switching one beside a partner of the
opposite declared polarity, which is then not split in two; the derived child names its partner and the decision; the
validator passes it in place of its parent and lets the two partners keep the same files (T-UNIQ) only as partners;
the plan makes one run unit per partner and the approval records the partner; the runner never compares the two in the
cross-unit check, and their shared raw data stay until both have ended.

Only the eval fixtures' public accession (ST002747) and synthetic names are used. Nothing is downloaded and no Console
runs.
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402,F401  (puts scripts/ on the path)
import decision_records  # noqa: E402
import test_campaign_contract as contract  # noqa: E402
import test_campaign_curated_selection as cs  # noqa: E402
import test_campaign_polarity_split as tps  # noqa: E402
from campaign import curated, plan, policy, ports  # noqa: E402

POS, NEG = tps.SPLIT_UNIT, "AN004457"  # AN004456 is declared Positive, AN004457 Negative in the fixture


def partner_docs(*, ion: str = "positive", partner_ion: str | None = "negative") -> dict:
    """switching_docs with AN004456 held on polarity switching at unit ion mode `ion`, and AN004457 (unless
    partner_ion is None) leaving out the same files for the same question at `partner_ion`."""
    docs = tps.switching_docs()
    docs[POS]["unit_technical"]["ion_mode"] = ion
    if partner_ion is not None:
        other = copy.deepcopy(docs[POS])
        for key in ("unit_id", "source_subrecord_id", "catalog_handoff_sha256"):
            other[key] = docs[NEG][key]
        other["unit_technical"]["ion_mode"] = partner_ion
        other["unit_technical"]["declared"] = dict(docs[NEG]["unit_technical"].get("declared") or {})
        docs[NEG] = other
        # AN004457's own (negative) files are kept by no unit of this fixture now: left out as other everywhere.
        for doc in docs.values():
            for item in doc["excluded_rows"]:
                if item.get("belongs_to_unit") == NEG:
                    item.update(reason="other", belongs_to_unit=None,
                                detail="Synthetic: these files are outside the fixture's partner pair.")
    return docs


def write_partner_triage(root: Path, docs: dict, code: "decision_records.Code", *, declared: dict | None = None) -> Path:
    """The accession's triage with AN004457's Catalog rows the same files as AN004456's (one set of switching files
    registered twice), each unit declared as `declared` says (default Positive / Negative), and records beside."""
    triage_root = cs.write_triage(root, docs)
    folder = triage_root / cs.ACCESSION
    catalog_path = folder / "inputs" / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    units = {unit["unit_id"]: unit for unit in catalog["units"]}
    units[NEG]["rows"] = copy.deepcopy(units[POS]["rows"])
    for unit_id, mode in (declared or {POS: "Positive", NEG: "Negative"}).items():
        units[unit_id]["declared"]["ion_mode"] = mode
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    for unit in docs:
        decision_records.write_record(folder / f"{unit}.json", code,
                                      inputs=[{"role": "archive_listing", "path": "inputs/archive-members.tsv"}])
    return triage_root


class ValidatorTests(tps._WithCode, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.module = curated.validator_module()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_the_rule_reaches_a_unit_registered_at_one_polarity(self) -> None:
        docs = partner_docs()
        derivations, why = self.module.polarity_derivations(docs[POS], docs)
        self.assertEqual((derivations, why), ([("positive", {"partner_unit_id": NEG})], ""))
        self.assertEqual(self.module.polarity_derivations(docs[NEG], docs)[0], [("negative", {"partner_unit_id": POS})])
        self.assertIn("not switching", self.module.polarity_split_refusal(docs[POS], docs), "never split in two")
        alone = partner_docs(partner_ion=None)
        self.assertEqual(self.module.polarity_derivations(alone[POS], alone)[0], [("positive", {"partner_unit_id": None})],
                         "without a partner the partner is null")

    def test_a_switching_unit_beside_a_partner_of_the_other_declared_polarity_runs_at_its_own(self) -> None:
        docs = partner_docs(ion="switching", partner_ion="switching")
        docs[POS]["unit_technical"]["declared"] = {**(docs[POS]["unit_technical"].get("declared") or {}), "ion_mode": "Positive"}
        docs[NEG]["unit_technical"]["declared"] = {**(docs[NEG]["unit_technical"].get("declared") or {}), "ion_mode": "Negative"}
        self.assertEqual(self.module.polarity_derivations(docs[POS], docs)[0], [("positive", {"partner_unit_id": NEG})])
        self.assertIn("own polarity beside its partner", self.module.polarity_split_refusal(docs[POS], docs))
        self.assertEqual(self.module.polarity_split_refusal(docs[POS]), "", "without the accession, as in 2026-11-19")
        # Both declared Both: no own polarity, each is split in two as before.
        for unit in (POS, NEG):
            docs[unit]["unit_technical"]["declared"]["ion_mode"] = "Both"
        self.assertEqual([mode for mode, _ in self.module.polarity_derivations(docs[POS], docs)[0]], ["positive", "negative"])

    def test_a_partner_of_the_same_polarity_or_a_unit_held_on_more_is_not_reached(self) -> None:
        same = partner_docs(partner_ion="positive")
        self.assertIn("same files at no negative polarity", self.module.polarity_derivations(same[POS], same)[1])
        docs = partner_docs()
        docs[POS]["needs_person"].append({"code": "other", "question": "?"})
        self.assertIn("not polarity_switching alone", self.module.polarity_derivations(docs[POS], docs)[1])
        both = partner_docs(ion="both", partner_ion=None)
        self.assertTrue(self.module.polarity_derivations(both[POS], both)[1])

    def test_the_child_names_its_partner_and_the_decision(self) -> None:
        docs = partner_docs()
        child = self.module.derive_polarity_split(docs[POS], "positive", triage_relative_path="x/y.json",
                                                  triage_sha256="a" * 64, triage_record_sha256=None, partner_unit_id=NEG)
        self.assertEqual(child["polarity_split"], {"ion_mode": "positive", "reason": "polarity_switching",
                                                   "parent_unit_id": POS, "sibling_ion_mode": "negative",
                                                   "partner_unit_id": NEG})
        self.assertEqual(child["derived_from"]["decision"]["date"], "2026-11-20")
        self.assertEqual(child["status"], "ready")
        self.assertEqual(self.module.polarity_split_shape_problems(child), [])
        schema = self.module.load_schema(self.module.SCHEMA_V2_ID)
        self.assertEqual(self.module.schema_errors(child, schema, schema), [])
        for bad in (POS, "", 7):
            broken = copy.deepcopy(child)
            broken["polarity_split"]["partner_unit_id"] = bad
            self.assertTrue(any("partner_unit_id" in p for p in self.module.polarity_split_shape_problems(broken)), bad)
        broken = copy.deepcopy(child)
        broken["derived_from"]["decision"] = dict(self.module.POLARITY_SPLIT_DECISION)
        self.assertTrue(any("derived_from" in p for p in self.module.polarity_split_shape_problems(broken)))

    def children(self, triage_root: Path, docs: dict) -> dict[str, Path]:
        folder = triage_root / cs.ACCESSION
        out = {}
        for unit in (POS, NEG):
            derivations, _ = self.module.polarity_derivations(docs[unit], docs)
            for mode, options in derivations:
                child = self.module.derive_polarity_split(
                    docs[unit], mode, triage_relative_path=f"{cs.ACCESSION}/{unit}.json",
                    triage_sha256=curated.sha256_file(folder / f"{unit}.json"),
                    triage_record_sha256=curated.sha256_file(folder / f"{unit}.record.json"), **options)
                path = self.root / f"{unit}-{mode}.polarity-split.json"
                path.write_text(json.dumps(child), encoding="utf-8")
                out[f"{unit}-{mode}"] = path
        return out

    def test_each_partner_passes_in_place_of_its_parent_and_both_together_pass_t_uniq(self) -> None:
        docs = partner_docs()
        triage_root = write_partner_triage(self.root, docs, self.code)
        folder = triage_root / cs.ACCESSION
        paths = self.children(triage_root, docs)
        self.assertEqual(sorted(paths), [f"{POS}-positive", f"{NEG}-negative"])
        for unit, key in ((POS, f"{POS}-positive"), (NEG, f"{NEG}-negative")):
            validation = curated.run_validator(folder, polarity_split=[paths[key]])
            self.assertEqual(curated.polarity_split_verdict(validation, unit), ("pass", ""),
                             curated.finding_lines(validation["report"], 10))
        together = curated.run_validator(folder, polarity_split=list(paths.values()))
        for unit in (POS, NEG):
            self.assertEqual(curated.polarity_split_verdict(together, unit), ("pass", ""),
                             curated.finding_lines(together["report"], 10))
        self.assertFalse([item for item in together["report"]["findings"] if item["check"] == "T-UNIQ"])

    def test_a_child_claiming_a_partner_its_parent_does_not_have_is_refused(self) -> None:
        docs = partner_docs()
        triage_root = write_partner_triage(self.root, docs, self.code)
        path = self.children(triage_root, docs)[f"{POS}-positive"]
        child = json.loads(path.read_text(encoding="utf-8"))
        child["polarity_split"]["partner_unit_id"] = "AN004454"
        path.write_text(json.dumps(child), encoding="utf-8")
        verdict, said = curated.polarity_split_verdict(
            curated.run_validator(triage_root / cs.ACCESSION, polarity_split=[path]), POS)
        self.assertEqual(verdict, "error")
        self.assertIn("T-SPLIT", said)

    def test_two_units_split_in_two_over_the_same_files_are_still_refused_by_t_uniq(self) -> None:
        """Without opposite declared polarities nothing makes them partners: each is split in two, and the two
        positive children keep the same files (T-UNIQ)."""
        docs = partner_docs(ion="switching", partner_ion="switching")
        for unit in (POS, NEG):
            docs[unit]["unit_technical"]["declared"] = {**(docs[unit]["unit_technical"].get("declared") or {}), "ion_mode": "Both"}
        triage_root = write_partner_triage(self.root, docs, self.code, declared={POS: "Both", NEG: "Both"})
        paths = self.children(triage_root, docs)
        self.assertEqual(len(paths), 4)
        validation = curated.run_validator(triage_root / cs.ACCESSION,
                                           polarity_split=[paths[f"{POS}-positive"], paths[f"{NEG}-positive"]])
        self.assertTrue([item for item in validation["report"]["findings"] if item["check"] == "T-UNIQ"])
        self.assertEqual(curated.polarity_split_verdict(validation, POS)[0], "error")


@unittest.skipUnless(contract.AVAILABLE, "the Interactive and Catalog checkouts are not where this test looks")
class PlanTests(tps._WithCode, cs._Root):
    def setUp(self) -> None:
        super().setUp()
        self.database = self.root / "catalog" / "catalog.sqlite"
        self.database.parent.mkdir()
        cs.build_catalog(self.database)
        self.campaign_dir = self.root / "campaign"

    def manifest(self, triage_dir: Path) -> dict:
        catalog = ports.read_only_catalog(self.database)
        try:
            return plan.build_manifest(
                catalog, pool="declared", campaign_id="own", analysis_purpose="annotation",
                workspace_root=self.workspace_root, raw_retention_policy="delete_after_validated_output",
                pins={"catalog": {"version": "0.6.1"}, "libraries": [], "interactive": {"version": "0.5.71"}},
                profile=None, campaign_policy=policy.CampaignPolicy(),
                class_decision=lambda unit_id: ports.decide_class(catalog, unit_id, "annotation"),
                catalog_database=str(self.database), triage_dir=triage_dir, polarity_split_dir=self.campaign_dir,
            )
        finally:
            catalog.close()

    def test_each_partner_becomes_one_run_unit_at_its_own_polarity(self) -> None:
        manifest = self.manifest(write_partner_triage(self.root, partner_docs(), self.code))
        units = {unit.get("unit_key", unit["unit_id"]): unit for unit in manifest["units"]}
        self.assertIn(f"{POS}-pos", units, manifest["exclusions"])
        self.assertIn(f"{NEG}-neg", units)
        for key in (POS, NEG, f"{POS}-neg", f"{NEG}-pos"):
            self.assertNotIn(key, units)
        pos, neg = units[f"{POS}-pos"], units[f"{NEG}-neg"]
        self.assertEqual((pos["ion_mode"], neg["ion_mode"]), ("Positive", "Negative"))
        self.assertEqual(pos["group_id"], neg["group_id"], "the partners share their download group")
        self.assertEqual(pos["polarity_split"], {"parent_unit_id": POS, "ion_mode": "positive",
                                                 "sibling_unit_key": f"{NEG}-neg", "partner_unit_id": NEG})
        self.assertEqual(neg["polarity_split"]["sibling_unit_key"], f"{POS}-pos")
        for unit, mode, partner in ((pos, "positive", NEG), (neg, "negative", POS)):
            chosen = unit["curated_selection"]
            self.assertEqual(chosen["polarity_split"]["partner_unit_id"], partner)
            self.assertEqual(chosen["polarity_split"]["parent_unit_id"], unit["unit_id"])
            self.assertEqual(chosen["validator"]["verdict"], "pass")
            self.assertEqual(chosen["derived_from"]["decision"]["date"], "2026-11-20")
        self.assertTrue(manifest["triage"]["polarity_split"][POS]["own_polarity"])
        totals = manifest["totals"]["polarity_split"]
        self.assertEqual((totals["parents"], totals["children"], totals["own_polarity"]), (2, 2, 2))
        self.assertEqual(totals["parent_known_bytes"], pos["known_bytes"], "the shared files counted once")
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("own polarity: 2 of them", text)
        self.assertIn(f"{POS} -> {POS}-pos (own polarity, partner {NEG} as {NEG}-neg", text)
        events = plan.decision_events(manifest, "sha256:" + "0" * 64, {"approval_id": "A", "covers": ["1", "3", "4", "5"]})
        planned = {key: detail for kind, detail, key in events if kind == "polarity_split_planned"}
        self.assertEqual(sorted(planned), [f"{POS}-pos", f"{NEG}-neg"])
        self.assertEqual(planned[f"{NEG}-neg"]["partner_unit_id"], POS)
        self.assertEqual(planned[f"{NEG}-neg"]["decision"]["date"], "2026-11-20")


def partner_world(root: Path, units: dict, groups: dict) -> "fakes.World":
    """cs.curated_world, with P-pos and N-neg planned as partner units (their manifest entries carry polarity_split
    naming each other, as plan._curated_units writes it): two Catalog units over the same files."""
    original = fakes.World.__init__

    def init(self, world_root, unit_ids, **options):
        for unit in (options.get("manifest") or {}).get("units") or []:
            key = unit["unit_key"]
            if key in ("P-pos", "N-neg"):
                mine, theirs = ("P", "N") if key == "P-pos" else ("N", "P")
                mode = "positive" if key == "P-pos" else "negative"
                unit["polarity_split"] = {"parent_unit_id": mine, "ion_mode": mode, "partner_unit_id": theirs,
                                          "sibling_unit_key": "N-neg" if key == "P-pos" else "P-pos"}
        original(self, world_root, unit_ids, **options)

    with mock.patch.object(fakes.World, "__init__", init):
        return cs.curated_world(root, units, groups=groups)


class RunnerTests(cs._Root):
    PARTNERS = {"P-pos": {"catalog": "P", "workspace": "P-pos"}, "N-neg": {"catalog": "N", "workspace": "N-neg"}}

    def test_partners_are_never_compared_with_each_other(self) -> None:
        world = partner_world(self.root, dict(self.PARTNERS), {"g": ["P-pos", "N-neg"]})
        world.run()
        self.assertEqual([cs.unit_row(world, key)["state"] for key in ("P-pos", "N-neg")], ["done", "done"])
        self.assertEqual(world.interactive.cross_unit_calls, [], "rule D would refuse the second partner")
        reasons = {item["detail"]["reason"] for item in cs.events(world, "cross_unit_verdict")}
        self.assertEqual(reasons, {"polarity_split_sibling"})

    def test_the_shared_raw_data_go_only_after_both_partners_have_downloaded_and_each_has_ended(self) -> None:
        world = partner_world(self.root, dict(self.PARTNERS), {"g": ["P-pos", "N-neg"]})
        world.gate.exits.update(pre_cleanup=0, final=0)
        world.run()
        rows = {key: cs.unit_row(world, key) for key in ("P-pos", "N-neg")}
        self.assertEqual({row["raw_disposition"] for row in rows.values()}, {"released"})
        order = [(kind, call) for kind, call in world.interactive.calls if kind in ("download", "cleanup", "discard")]
        downloads = [index for index, (kind, _call) in enumerate(order) if kind == "download"]
        deletions = [(index, call.get("manifest_path")) for index, (kind, call) in enumerate(order) if kind in ("cleanup", "discard")]
        self.assertEqual(len(downloads), 2)
        self.assertGreater(min(index for index, _path in deletions), max(downloads),
                           "no partner's raw data go before both partners downloaded")
        self.assertEqual(sorted(path for _index, path in deletions),
                         sorted(row["manifest_path"] for row in rows.values()), "each partner deletes only its own tree")
        self.assertEqual(len(world.interactive.fetches), 1, "the shared object is fetched once")

    def test_a_partner_excluded_at_its_preflight_leaves_the_other_running(self) -> None:
        world = partner_world(self.root, dict(self.PARTNERS), {"g": ["P-pos", "N-neg"]})
        world.scripts["N-neg"] = fakes.UnitScript(disposition="exclude")
        world.run()
        self.assertEqual((cs.unit_row(world, "P-pos")["state"], cs.unit_row(world, "N-neg")["state"]), ("done", "excluded"))

    def test_units_naming_no_partner_are_still_compared(self) -> None:
        """Without polarity_split the two units of one group over the same files go to the cross-unit check."""
        world = cs.curated_world(self.root, dict(self.PARTNERS), groups={"g": ["P-pos", "N-neg"]})
        world.run()
        self.assertTrue(world.interactive.cross_unit_calls)


if __name__ == "__main__":
    unittest.main()
