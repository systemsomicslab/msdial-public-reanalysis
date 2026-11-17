"""The runner against Interactive 0.5.69's own curated-selection code (WS0, 2026-11-16).

tests/campaign_fakes.py plays Interactive for the runner's state machine; this module replaces the parts of the fake
that decide a curated selection with the real functions of the pinned Interactive (MSDIAL_INTERACTIVE_ROOT, the #102
worktree or a git archive of its head): the download holds the unit to its triage through
mcp_server._curated_selection_context and curated_selection.apply_curated_selection (with the workspace_unit_key
validation of repository_reanalysis), the preflight compares the headers with reconcile_curated_selection, and the
cross-unit check is the msdial_repository_cross_unit_check tool itself, which reads and writes the unit manifests on
disk. The selection is msdial-selection-canonical.v1, computed by both sides and by the doc's reference
implementation. What is asserted is the runner's side: the arguments it sends, how it reads the real replies, and the
decision-ledger and review-log entries it writes from them.

The accessions are the public eval fixtures ST002747 and ST002283; every file is synthetic (no byte of raw data, no
download, no MS-DIAL run, no Catalog write). The tests are skipped where the pinned Interactive predates 0.5.69.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes  # noqa: E402  (puts scripts/ on the path)
import test_campaign_contract as contract  # noqa: E402  (puts Interactive and the Catalog on the path)
import test_campaign_curated_selection as cs  # noqa: E402
import triage_eval as te  # noqa: E402
from campaign import curated, plan, policy, ports  # noqa: E402

try:
    from msdial_app import curated_selection as ia  # noqa: E402
    from msdial_app import mcp_server as ia_tools  # noqa: E402
    from msdial_app import repository_reanalysis as ia_rr  # noqa: E402
    REAL = contract.AVAILABLE and hasattr(ia, "selection_canonical") and hasattr(ia_tools, "msdial_repository_cross_unit_check")
except ImportError:  # pragma: no cover - an Interactive before 0.5.69, or none
    REAL = False

# The doc's worked example (docs/llm-assisted-curation.md section 3.1) and its two hashes.
DOC_EXAMPLE = {
    "triage": {"unit_id": "AN000001",
               "rows": [{"row_index": 2, "member_path": "study\\POS\\S02.mzML", "archive": "ST000001_rawdata.zip"},
                        {"row_index": 1, "member_path": "study/POS/S01.mzML", "archive": "ST000001_rawdata.zip"}],
               "added_members": [{"member_path": "study/POS/QC01.mzML", "archive": "ST000001_rawdata.zip"}]},
    "listing": {"study/pos/s01.mzml": {"archive": "ST000001_rawdata.zip", "crc32": "abcdef01", "size": 1234},
                "study/pos/s02.mzml": {"archive": "ST000001_rawdata.zip", "crc32": "0BCDEF2", "size": 5678}},
}
DOC_SHA_WITH_LISTING = "4c609902ea5315cf4ab6886cbfdd72ca45ff2892c172e4060822975b8ef10804"
DOC_SHA_WITHOUT_LISTING = "e33f229c94650c9b06bb1fa48c9180923aa029ab38af41ad325e4d131138898f"
DOC_PATH = Path(os.environ.get("MSDIAL_LLM_CURATION_DOC")
                or TESTS.parent.parent / "llm-transparency" / "docs" / "llm-assisted-curation.md")
REPOSITORY = "metabolomics_workbench"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- the selection: one value on three sides -------------------------------------------------------------------------

class SelectionCanonicalTests(unittest.TestCase):
    """WS0 decision 1: the runner's copy of msdial-selection-canonical.v1, Interactive's selection_canonical and the
    doc's reference implementation give one value."""

    def test_the_doc_worked_example_gives_the_doc_hashes_on_the_runner_side(self) -> None:
        triage, listing = DOC_EXAMPLE["triage"], DOC_EXAMPLE["listing"]
        self.assertEqual(curated.selection_sha256(triage, listing), DOC_SHA_WITH_LISTING)
        self.assertEqual(curated.selection_sha256(triage, {}), DOC_SHA_WITHOUT_LISTING)
        entries = curated.selection_canonical(triage, listing)
        self.assertEqual([(item["member_path"], item["crc32"], item["size"], item["sample_row_index"]) for item in entries],
                         [("study/POS/QC01.mzML", None, None, None), ("study/POS/S01.mzML", "ABCDEF01", 1234, 1),
                          ("study/POS/S02.mzML", "00BCDEF2", 5678, 2)])
        self.assertEqual(curated.SELECTION_CANONICAL_FORM, "msdial-selection-canonical.v1")

    @unittest.skipUnless(DOC_PATH.is_file(), "the curation doc (PR #59) is not where this test looks")
    def test_the_doc_itself_states_the_example_and_hashes_the_runner_gives(self) -> None:
        text = DOC_PATH.read_text(encoding="utf-8")
        example = json.loads(re.search(r"```json selection-example\n(.*?)```", text, re.S).group(1))
        stated = re.search(r"```text selection-example-sha256\n([0-9a-f]{64})", text).group(1)
        self.assertEqual(curated.selection_sha256(example["triage"], example["listing"]), stated)
        self.assertIn(DOC_SHA_WITHOUT_LISTING, text)

    @unittest.skipUnless(REAL, "the pinned Interactive has no msdial-selection-canonical.v1 (before 0.5.69)")
    def test_interactive_and_the_runner_agree_on_the_eval_fixtures(self) -> None:
        self.assertEqual(ia.SELECTION_CANONICAL_FORM, curated.SELECTION_CANONICAL_FORM)
        self.assertEqual(ia.selection_sha256(DOC_EXAMPLE["triage"], DOC_EXAMPLE["listing"]), DOC_SHA_WITH_LISTING)
        compared = 0
        for accession in sorted(path.name for path in (TESTS / "fixtures" / "triage-eval").iterdir() if path.is_dir()):
            data = (te.fixture_dir(accession) / "archive-members.tsv").read_bytes()
            listing = curated.parse_archive_listing(data)
            self.assertEqual(listing, ia.parse_archive_listing(data), accession)
            for docs in (te.build_good(accession), te.stage3b_style(accession)):
                for triage in docs.values():
                    for child in [None] + list(triage.get("children") or []):
                        for bound in (listing, {}):
                            with self.subTest(accession=accession, unit=triage["unit_id"], child=(child or {}).get("child_key")):
                                self.assertEqual(curated.selection_canonical(triage, bound, child),
                                                 ia.selection_canonical(triage, bound, child))
                                compared += 1
        self.assertGreater(compared, 50)

    @unittest.skipUnless(REAL, "the pinned Interactive has no msdial-selection-canonical.v1 (before 0.5.69)")
    def test_a_tie_on_every_sort_key_is_ordered_by_the_canonical_json_on_both_sides(self) -> None:
        added = [{"member_path": "QC/QC01.mzML", "archive": "ST1_rawdata.zip"},
                 {"member_path": "QC/QC01.mzML", "archive": "ST1_rawdata2.zip"}]
        one = {"unit_id": "AN1", "rows": [], "added_members": added}
        other = {"unit_id": "AN1", "rows": [], "added_members": list(reversed(added))}
        self.assertEqual(curated.selection_sha256(one), curated.selection_sha256(other))
        self.assertEqual(curated.selection_sha256(one), ia.selection_sha256(other))

    @unittest.skipUnless(REAL, "the pinned Interactive has no msdial-analysis-csv-canonical.v1 (before 0.5.69)")
    def test_the_replay_canonical_csv_is_interactives(self) -> None:
        raw = "D:\\ws\\metabolomics_workbench\\ST1\\AN1\\raw"
        cases = [
            f"File path,File name\r\n{raw}\\POS\\S1.mzML,S1\r\n",
            f"File path,File name\n{raw}\\..\\converted\\S1.mzML,S1\n".replace("\\..\\", "\\"),
            "File path,File name\nD:\\ws\\metabolomics_workbench\\ST1\\AN1\\converted\\S1.mzML,S1\n\n",
            "File path,File name\nE:\\elsewhere\\S1.mzML,S1\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(curated.canonical_analysis_csv(text.encode("utf-8"), raw),
                                 ia.canonical_analysis_csv(text.encode("utf-8"), raw))
        self.assertIn(b"../converted/S1.mzML", curated.canonical_analysis_csv(cases[2].encode("utf-8"), raw))
        self.assertIn(b"<external>/S1.mzML", curated.canonical_analysis_csv(cases[3].encode("utf-8"), raw))


# ---- Interactive's own code behind the fake's tool surface ---------------------------------------------------------

class DiskStore(campaign_fakes.ManifestStore):
    """Unit manifests kept on disk as well, whole, so Interactive's own tools can read and update them."""

    def write(self, path, value) -> None:  # type: ignore[override]
        super().write(path, value)
        location = Path(str(path))
        location.parent.mkdir(parents=True, exist_ok=True)
        location.write_text(json.dumps(value, indent=1, sort_keys=True), encoding="utf-8")

    def reload(self, path) -> None:
        self.values[self.key(path)] = json.loads(Path(str(path)).read_text(encoding="utf-8"))


class RealInteractive(campaign_fakes.FakeInteractive):
    """FakeInteractive with Interactive 0.5.69's curated-selection decisions made by its own code."""

    def __init__(self, world: campaign_fakes.World, *, headers: dict[str, tuple[str, str]] | None = None) -> None:
        super().__init__(world)
        self.store = DiskStore()
        # Per member path (case-folded): the (polarity, acquisition mode) its raw header states, where a test makes it
        # disagree with the triage; every other kept member's header states what its row says.
        self.headers = {key.casefold(): value for key, value in (headers or {}).items()}
        self.listings: dict[str, Path] = {}
        self.real_cross_unit_replies: list[dict[str, Any]] = []

    def _approved_entry(self, key: str) -> dict[str, Any]:
        """campaign_selection's reading of the approved campaign manifest, made from the world's approval."""
        manifest = json.loads(Path(self.world.campaign["manifest_path"]).read_text(encoding="utf-8"))
        entry = next((item for item in manifest.get("units") or [] if str(item.get("unit_key") or item.get("unit_id")) == key), None)
        return {"approval_id": self.world.approval["approval_id"], "manifest_digest": self.world.digest,
                "manifest_named": True, "entry": entry}

    def _apply_curated(self, arguments: dict[str, Any]) -> dict[str, Any]:
        handoff_path = str(arguments["handoff_path"])
        handoff = json.loads(Path(handoff_path).read_text(encoding="utf-8-sig"))
        catalog_unit = str(handoff.get("analysis_unit_id") or "")
        try:
            if arguments.get("workspace_unit_key"):
                ia_rr.validate_workspace_unit_key(arguments["workspace_unit_key"], catalog_unit)
            context = ia_tools._curated_selection_context(
                arguments["curated_selection_path"], handoff, handoff_path,
                child_key=str(arguments.get("curated_selection_child_key") or ""),
                original_handoff_sha256=str(arguments.get("original_handoff_sha256") or ""),
                original_handoff_path=str(arguments.get("original_handoff_path") or ""),
                validator=arguments.get("curated_selection_validator"),
                decision_record_path=str(arguments.get("decision_record_path") or ""))
            key = f"{catalog_unit}-{context['child_key']}" if context["child_key"] else catalog_unit
            context["campaign"] = self._approved_entry(key)
            samples = [{"sample_id": str(item.get("sample_id") or ""), "raw_file": str(item.get("raw_file") or ""), "values": {}}
                       for item in handoff.get("sample_metadata") or []]
            applied = ia.apply_curated_selection(handoff=handoff, samples=samples, **context)
        except ia.CuratedSelectionError as error:
            return error.failure()
        except ValueError as error:
            return {"ok": False, "reason": "validation_error", "error_type": "ValueError", "detail": str(error)}
        return applied["provenance"]

    def _reconcile(self, manifest_path: str, applied: dict[str, Any], mode: str) -> dict[str, Any]:
        """The lineage and header preflight a real lease and extractor would record for the kept rows, then
        reconcile_curated_selection over the manifest. Each input lies under the unit's raw directory and names its
        archive member and the archive listing (for the cross-unit check's listed content)."""
        current = self.store.read(manifest_path) or {}
        workspace = Path(str(current.get("workspace")))
        data_root = workspace / "raw" / "data"
        technical = applied.get("applied_technical") or {}
        rows, per_file = [], []
        for row in applied.get("rows_kept") or []:
            member = str(row["member_path"])
            path = str(data_root / Path(member))
            listing = self.listings.get("*")
            rows.append({"path": path, "kind": "archive_member", "sample_id": row.get("sample_id"),
                         "acquisition_type": str(technical.get("acquisition_mode") or "").upper(),
                         "source": {"archive": {"url": f"https://repository.example/{row.get('archive') or 'archive.zip'}"}},
                         "basis": {"listing": [{"member": member, **({"members_tsv": str(listing)} if listing else {})}]}})
            ion = str(row.get("ion_mode") or technical.get("ion_mode") or "")
            acquisition = str(row.get("acquisition_mode") or technical.get("acquisition_mode") or "")
            polarity, mode_found = self.headers.get(member.casefold(), (ion.capitalize(), acquisition.upper()))
            per_file.append({"file": path, "outcome": "ok", "polarity": polarity, "acquisition_mode": mode_found})
        current.update(input_directory=str(data_root), input_lineage={"rows": rows},
                       raw_metadata_preflight={"summary": {"per_file": per_file}})
        self.store.write(manifest_path, current)
        block = ia.reconcile_curated_selection(current)
        current["curated_selection_reconciliation"] = block
        self.store.write(manifest_path, current)
        return block

    def cross_unit_check(self, *, manifest_paths: list[str]) -> dict[str, Any]:
        """The real msdial_repository_cross_unit_check over the manifests on disk (record=True), which writes each
        manifest's cross_unit_attribution; the store is read back from disk after it."""
        self.calls.append(("cross_unit_check", {"manifest_paths": list(manifest_paths)}))
        for path in manifest_paths:
            self.store.write(path, self.store.read(path) or {})
        result = ia_tools.msdial_repository_cross_unit_check(manifest_paths=list(manifest_paths), record=True)
        if result.get("ok") is not False:
            self.cross_unit_calls.append(list(manifest_paths))
            self.real_cross_unit_replies.append(copy.deepcopy(result))
            for path in manifest_paths:
                self.store.reload(path)
        return result


def triage_world(root: Path, accession: str, docs: dict[str, dict], *, record: bool, headers=None,
                 keys: list[str] | None = None, ratified: dict[str, dict | None] | None = None,
                 proposal_changes: dict[str, dict] | None = None) -> tuple[campaign_fakes.World, dict[str, dict]]:
    """A World whose approved manifest plans `docs` (one unit each, all in one download group) from triage files under
    <root>\\triage\\<accession>, each made from the handoff the fake Catalog writes for its unit (with the accession,
    as a real handoff carries it), with Interactive's code behind the fake. `record` writes each triage's decision
    record binding the accession's archive listing. Returns the world and the planned manifest entries.

    `ratified` (WS0 decision A), by unit id: the fake Catalog writes handoffs of the Catalog's shape (sidecars at
    absolute paths, the Class proposal with the ratification the runner saves at boundary 3), and each triage is made
    from such a handoff carrying the ratification record named here (another campaign's, or None for none), kept at
    inputs\\<unit_id>\\handoff.json and, with `record`, bound as the record's catalog_handoff input. `proposal_changes`
    changes a unit's triaged proposal."""
    fixture = json.loads((te.fixture_dir(accession) / "catalog.json").read_text(encoding="utf-8"))
    rows = {unit["unit_id"]: unit["rows"] for unit in fixture["units"]}
    extra = {unit_id: {"accession": accession, "repository": REPOSITORY} for unit_id in docs}
    folder = root / "triage" / accession
    inputs = folder / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    listing = inputs / "archive-members.tsv"
    listing.write_bytes((te.fixture_dir(accession) / "archive-members.tsv").read_bytes())
    planned = []
    shaped = ratified is not None
    for unit_id, doc in docs.items():
        if shaped:
            handoff = campaign_fakes.catalog_shaped_handoff(
                root / "run" / "catalog-data" / "handoffs", unit_id, campaign_fakes.World.class_digest(unit_id),
                rows[unit_id], (ratified or {}).get(unit_id), extra[unit_id])
            handoff["class_proposal"].update((proposal_changes or {}).get(unit_id) or {})
            (inputs / unit_id).mkdir(parents=True, exist_ok=True)
            (inputs / unit_id / "handoff.json").write_bytes(campaign_fakes.handoff_bytes(handoff))
        else:
            handoff = campaign_fakes.fake_handoff(unit_id, campaign_fakes.World.class_digest(unit_id), rows[unit_id], extra[unit_id])
        doc = copy.deepcopy(doc)
        doc["catalog_handoff_sha256"] = _sha(campaign_fakes.handoff_bytes(handoff))
        path = folder / f"{unit_id}.json"
        path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        sha = _sha(path.read_bytes())
        bound: dict[str, Any] = {}
        record_sha = None
        if record:
            record_path = curated.record_path(path)
            record_path.write_text(json.dumps({
                "schema": "msdial-llm-decision-record.v1", "decision_id": sha, "kind": "unit_triage", "accession": accession,
                "unit_id": unit_id, "payload": {"path": path.name, "schema": doc["schema"], "sha256": sha},
                "agent": {"harness": "synthetic-harness", "model_id": "synthetic-model"},
                "inputs": [{"role": "archive_listing", "path": "inputs/archive-members.tsv", "sha256": _sha(listing.read_bytes()),
                            "bytes": listing.stat().st_size}]
                + ([{"role": "catalog_handoff", "path": f"inputs/{unit_id}/handoff.json",
                     "sha256": doc["catalog_handoff_sha256"]}] if shaped else [])}), encoding="utf-8")
            bound = curated.record_inputs(record_path, sha)["listing"]
            record_sha = _sha(record_path.read_bytes())
        planned.append({
            "unit_id": unit_id, "unit_key": unit_id, "accession": accession, "workspace_name": unit_id,
            "curated_selection": {
                "triage_path": str(path), "triage_relative_path": f"{accession}/{path.name}", "triage_sha256": sha,
                "triage_record_sha256": record_sha, "catalog_handoff_sha256": doc["catalog_handoff_sha256"],
                "catalog_rows_sha256": curated.rows_sha256(rows[unit_id]), "child_key": None,
                "selection_sha256": curated.selection_sha256(doc, bound),
                "selection_canonical_form": curated.SELECTION_CANONICAL_FORM,
                "validator": {"script_sha256": "v" * 64, "verdict": "pass", "report_sha256": "r" * 64},
            },
        })
    keys = keys or list(docs)
    world = campaign_fakes.World(root / "run", keys, manifest={"units": planned}, download_groups={"g": keys},
                                 handoff_rows=rows, handoff_extra=extra)
    world.interactive = RealInteractive(world, headers=headers)
    world.interactive.listings["*"] = listing
    world.catalog.catalog_shaped = shaped
    return world, {item["unit_key"]: item for item in planned}


def events(world: campaign_fakes.World, kind: str | None = None) -> list[dict]:
    return cs.events(world, kind)


def unit_row(world: campaign_fakes.World, key: str) -> dict:
    return cs.unit_row(world, key)


def exported(world: campaign_fakes.World) -> tuple[list[dict], str]:
    target = world.directory / curated.DECISION_LEDGER
    with world.open() as book:
        curated.export_decision_ledger(target, book.events())
    text = target.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()], text


ABSOLUTE = re.compile(r'"(?:[A-Za-z]:[\\/]|\\\\\\\\)')


@unittest.skipUnless(REAL, "the pinned Interactive predates 0.5.69's curated selection (set MSDIAL_INTERACTIVE_ROOT)")
class RunnerWithInteractiveTests(cs._Root):
    def test_st002747_runs_four_disjoint_selections_with_interactives_own_checks(self) -> None:
        docs = te.build_good("ST002747")
        world, planned = triage_world(self.root, "ST002747", docs, record=True)
        world.run()
        keys = list(planned)
        self.assertEqual({key: unit_row(world, key)["state"] for key in keys}, {key: "done" for key in keys})
        # The download carried what Interactive needs (WS0 decisions 2 and 7), and its own provenance is where the
        # runner reads it (decision 4).
        for sent in world.interactive.curated_downloads:
            self.assertTrue(Path(sent["decision_record_path"]).parent.samefile(self.root / "triage" / "ST002747"))
            self.assertTrue(sent["original_handoff_path"].endswith("handoff.original.json"))
        for key in keys:
            manifest = world.interactive.store.read(unit_row(world, key)["manifest_path"])
            block = manifest["project"]["repository_metadata"]["curated_selection"]
            self.assertEqual(block["schema"], ia.PROVENANCE_SCHEMA)
            self.assertEqual(block["selection_sha256"], planned[key]["curated_selection"]["selection_sha256"])
            self.assertTrue(block["selection_sha256_matches_manifest"])
            self.assertEqual(block["catalog_handoff_verification"], "original_read_and_copy_compared")
            self.assertEqual(block["original_handoff"]["source"], "original_handoff_path")
            self.assertEqual(block["triage_record"]["located"], "explicit")
            self.assertGreater(block["selection_listing_entries"], 0, "the record's listing gave crc32 and size")
        applied = {item["unit_key"]: item["detail"] for item in events(world, "curated_selection_applied")}
        self.assertEqual({key: item["selection_sha256_equal"] for key, item in applied.items()}, {key: True for key in keys})
        self.assertEqual({item["selection_canonical_form"] for item in applied.values()}, {"msdial-selection-canonical.v1"})
        # The real reconciliation, copied into the ledger in its nested shape (decision 3).
        reconciled = {item["unit_key"]: item["detail"] for item in events(world, "header_reconciliation")}
        for key in keys:
            self.assertEqual((reconciled[key]["verdict"], reconciled[key]["disagree"]), ("agree", 0))
            self.assertEqual(reconciled[key]["schema"], ia.RECONCILIATION_SCHEMA)
            item = reconciled[key]["files"][0]
            self.assertEqual(set(item["ion_mode"]), {"triage", "triage_basis", "header", "result"})
            self.assertFalse(re.match(r"^[A-Za-z]:", item["file"]), "Interactive's portable file names")
        # One real cross-unit check over the four manifests, read by position and by its own keys.
        self.assertEqual(len(world.interactive.real_cross_unit_replies), 1)
        reply = world.interactive.real_cross_unit_replies[0]
        self.assertEqual((reply["verdict"], reply["rule"]), ("passed", ia.CROSS_UNIT_RULE))
        verdict = events(world, "cross_unit_verdict")[0]["detail"]
        self.assertEqual((verdict["verdict"], verdict["refused"]), ("passed", {}))
        self.assertEqual(verdict["interactive"]["rule"], ia.CROSS_UNIT_RULE)
        self.assertEqual([item["verdict"] for item in verdict["unit_verdicts"]], ["passed"] * 4)
        for key in keys:
            manifest = world.interactive.store.read(unit_row(world, key)["manifest_path"])
            self.assertEqual(manifest["cross_unit_attribution"]["verdict"], "passed")
        lines, text = exported(world)
        self.assertEqual(curated.verify_decision_ledger(world.directory / curated.DECISION_LEDGER), [])
        self.assertFalse(ABSOLUTE.search(text), "the decision ledger holds workspace-relative paths only (decision 8)")
        self.assertFalse((self.root / "triage" / "ST002747" / curated.REVIEW_LOG).exists(), "nothing the doc does not allow")

    def test_a_header_against_the_triage_holds_the_unit_and_a_persons_answer_is_logged_as_the_doc_says(self) -> None:
        docs = te.build_good("ST002747")
        member = next(row["member_path"] for row in docs["AN004456"]["rows"] if row["role"] == "sample")
        world, _planned = triage_world(self.root, "ST002747", docs, record=False, headers={member: ("Negative", "DDA")})
        world.run()
        self.assertEqual(unit_row(world, "AN004456")["state"], "disposition_held")
        found = [item["detail"] for item in events(world, "header_reconciliation") if item["unit_key"] == "AN004456"][0]
        self.assertEqual((found["verdict"], found["reason"], found["disagreements_by_field"]),
                         ("disagree", "triage_header_disagreement", {"ion_mode": 1}))
        item = [entry for entry in found["files"] if entry["result"] == "disagree"][0]
        self.assertEqual((item["member_path"], item["ion_mode"]["header"], item["ion_mode"]["result"], item["reasons"]),
                         (member, "Negative", "disagree", ["polarity"]))
        # The siblings were judged without it, and ran.
        self.assertEqual({unit_row(world, key)["state"] for key in ("AN004454", "AN004455", "AN004457")}, {"done"})
        with world.open() as book:
            book.add_request("skip", "AN004456", "The header is right; this file is negative mode.", "Synthetic Person",
                             world.runner(book).stamp(), question_code="triage_header_disagreement")
        world.run()
        self.assertEqual(unit_row(world, "AN004456")["state"], "skipped")
        log = [json.loads(line) for line in (self.root / "triage" / "ST002747" / curated.REVIEW_LOG).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([entry["kind"] for entry in log], ["person_answer"])
        self.assertEqual({key: value for key, value in log[0].items() if key not in ("seq", "at", "kind", "decision_id", "prev_sha256")},
                         {"by": "Synthetic Person", "question_code": "triage_header_disagreement",
                          "answer": "The header is right; this file is negative mode.", "channel": "conversation"})
        answers = [line for line in exported(world)[0] if line["kind"] == "person_answer"]
        self.assertEqual((answers[0]["unit_key"], answers[0]["standing"]), ("AN004456", curated.PERSON_ANSWER))

    def test_st002283_whole_study_selections_are_refused_by_interactives_cross_unit_rules(self) -> None:
        """The stage3b shape: every unit of ST002283 keeps all of its polarity's files, HILIC and lipid alike. The real
        check refuses them (rule C: one set across two chromatographies), and the runner ends each with Interactive's
        own reason code, read from reasons, never retried and never run."""
        docs = te.stage3b_style("ST002283")
        world, planned = triage_world(self.root, "ST002283", docs, record=False)
        world.run()
        keys = list(planned)
        states = {key: unit_row(world, key) for key in keys}
        refused = {key for key, unit in states.items() if unit["terminal_reason"] == policy.ATTRIBUTION_REFUSED}
        self.assertTrue(refused)
        reply = world.interactive.real_cross_unit_replies[-1]
        said = {item["analysis_unit_id"]: item for item in reply["units"]}
        self.assertEqual(refused, {key for key in keys if said[key]["verdict"] == "refused"},
                         "the runner refuses exactly the units Interactive refused")
        for key in refused:
            detail = json.loads(states[key]["terminal_detail"])
            self.assertEqual(detail["reason_code"], said[key]["reasons"][0])
            self.assertIn(detail["reason_code"], ia.CROSS_UNIT_REFUSALS)
            self.assertEqual(states[key]["failures"], 0)
            self.assertNotIn((key, "run"), world.interactive.console_starts)
        self.assertIn(ia.CROSS_UNIT_INDISTINGUISHABLE_CHROMATOGRAPHY, {said[key]["reasons"][0] for key in refused})
        verdict = events(world, "cross_unit_verdict")[-1]["detail"]
        self.assertEqual(set(verdict["refused"]), refused)
        self.assertTrue(all(item["rule"] in ("P", "C", "S", "D") for item in verdict["refused"].values()))
        rules = {item["detail"].get("rule") for item in events(world, "terminal_refusal")}
        self.assertIn("C", rules)

    def test_a_listing_changed_after_approval_is_refused_by_interactive_and_ends_the_unit(self) -> None:
        docs = {"AN004454": te.build_good("ST002747")["AN004454"], "AN004455": te.build_good("ST002747")["AN004455"]}
        world, _planned = triage_world(self.root, "ST002747", docs, record=True)
        listing = self.root / "triage" / "ST002747" / "inputs" / "archive-members.tsv"
        listing.write_bytes(listing.read_bytes() + b"ST002747_rawdata.zip\textra/X.raw\t1\t00000001\n")
        world.run()
        for key in docs:
            unit = unit_row(world, key)
            self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("failed", policy.ATTRIBUTION_REFUSED, 0))
            self.assertEqual(json.loads(unit["terminal_detail"])["reason_code"], ia.CURATED_SELECTION_RECORD_MISMATCH)
        self.assertEqual(world.interactive.download_starts, [], "refused before any byte")

    def test_a_reopened_unit_gets_a_workspace_interactive_accepts(self) -> None:
        docs = {"AN004456": te.build_good("ST002747")["AN004456"], "AN004457": te.build_good("ST002747")["AN004457"]}
        world, planned = triage_world(self.root, "ST002747", docs, record=False)
        # The approval recorded AN004456 into a workspace of its own (plan --reopen): the runner passes it, and
        # Interactive's own validation accepts it (decision 5).
        with world.open() as book:
            for event in book.events("triage_recorded"):
                if event["unit_key"] == "AN004456":
                    detail = json.loads(event["detail_json"])
                    detail["workspace_name"] = "AN004456@again"
                    with book.transaction() as db:
                        db.execute("UPDATE campaign_event SET detail_json = ? WHERE seq = ?", (json.dumps(detail), event["seq"]))
        world.run()
        unit = unit_row(world, "AN004456")
        self.assertEqual(unit["state"], "done")
        self.assertTrue(unit["workspace"].endswith("AN004456@again"))
        sent = [item for item in world.interactive.curated_downloads if item.get("workspace_unit_key")]
        self.assertEqual([item["workspace_unit_key"] for item in sent], ["AN004456@again"])
        with self.assertRaises(ValueError):
            ia_rr.validate_workspace_unit_key("AN004457@again", "AN004456")


# ---- WS0 decision A: a handoff re-ratified under this campaign, against Interactive's own comparison -------------------

CAMPAIGN_A = {"kind": "campaign_approval", "authorization_schema": "msdial-campaign-authorization.v1", "boundary": 3,
              "approval_id": "appr-A", "manifest_digest": "sha256:" + "1" * 64, "authorization_sha256": "2" * 64,
              "campaign_id": "campaign-a", "proposal_id": "", "abstention": True,
              "ratified_at": "2026-11-16T02:00:00+00:00"}


def ratified_a(unit_id: str) -> dict:
    """Campaign A's ratification record of the unit's Class, as the Catalog embeds it in a handoff."""
    return {**CAMPAIGN_A, "proposal_id": campaign_fakes.World.class_digest(unit_id)}


@unittest.skipUnless(REAL, "the pinned Interactive predates 0.5.69's curated selection (set MSDIAL_INTERACTIVE_ROOT)")
class RatifiedHandoffAgainstInteractiveTests(cs._Root):
    """The triage names the handoff as the Catalog wrote it under campaign A's ratification; the runner ratifies the
    approved Class under its own approval (boundary 3) and takes the handoff again, which now embeds that ratification.
    The runner passes it only where Interactive's verify_handoff_copy (HANDOFF_RATIFICATION_KEYS) does, and holds it
    otherwise, before any byte moves."""

    def _write(self, name: str, value) -> Path:
        path = self.root / "written" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=1, sort_keys=True), encoding="utf-8")
        return path

    def test_the_runner_and_interactive_exclude_the_same_keys(self) -> None:
        self.assertEqual(curated.HANDOFF_RATIFICATION_KEYS, ia.HANDOFF_RATIFICATION_KEYS)
        self.assertEqual(tuple(curated.HANDOFF_COPY_REWRITTEN_KEYS), tuple(ia.HANDOFF_COPY_REWRITTEN_KEYS))

    def test_the_runner_decides_each_difference_as_interactive_does(self) -> None:
        rows = [{"sample_id": "S1", "raw_file": "S1"}, {"sample_id": "S2", "raw_file": "S2"}]
        triaged = campaign_fakes.catalog_shaped_handoff(self.root / "catalog", "AN1", "class-1", rows, ratified_a("AN1"))
        triaged_path = self._write("triaged.json", triaged)
        later = {**ratified_a("AN1"), "approval_id": "appr-B", "campaign_id": "campaign-b", "manifest_digest": "sha256:" + "3" * 64,
                 "authorization_sha256": "4" * 64, "ratified_at": "2026-11-17T03:00:00+00:00"}
        other_rows = self._write("other-rows.json", [{"sample_id": "x"}])
        same_rows = self._write("same-rows.json", rows)
        cases = {
            "re-ratified": (True, lambda value: value["class_proposal"].update(ratification=dict(later), ratified_by="appr-B")),
            "unchanged": (True, lambda value: None),
            "a sidecar moved, same rows": (True, lambda value: value.update(sample_table_path=str(same_rows))),
            "another assignment": (False, lambda value: value["class_proposal"]["assignments"][0].update(class_label="B")),
            "another ratified proposal": (False, lambda value: value["class_proposal"]["ratification"].update(proposal_id="x")),
            "an added ratification key": (False, lambda value: value["class_proposal"]["ratification"].update(note="x")),
            "no ratification": (False, lambda value: value["class_proposal"].update(ratification=None, ratified_by="")),
            "another unit attribute": (False, lambda value: value.update(unit_attributes={"Group": ["A"]})),
            "other sidecar rows": (False, lambda value: value.update(sample_table_path=str(other_rows))),
        }
        for index, (label, (expected, change)) in enumerate(cases.items()):
            with self.subTest(label):
                current = json.loads(json.dumps(triaged))
                change(current)
                current_path = self._write(f"current-{index}.json", current)
                runner = curated.handoff_difference(triaged_path, current_path)
                try:
                    found = ia.verify_handoff_copy(triaged_path, current)
                    interactive_passes = True
                except ia.CuratedSelectionError:
                    found, interactive_passes = None, False
                self.assertEqual((runner["same"], interactive_passes), (expected, expected))
                if found is not None:
                    self.assertEqual(runner["ratification"], found["ratification"])

    def test_st002747_reratified_by_the_runner_runs_with_both_ratifications_recorded(self) -> None:
        docs = te.build_good("ST002747")
        world, planned = triage_world(self.root, "ST002747", docs, record=True,
                                      ratified={unit_id: ratified_a(unit_id) for unit_id in docs})
        world.run()
        keys = list(planned)
        self.assertEqual({key: unit_row(world, key)["state"] for key in keys}, {key: "done" for key in keys})
        for key in keys:
            checked = [item["detail"] for item in events(world, "triage_checked") if item["unit_key"] == key][0]
            self.assertEqual(checked["handoff_identity"], "ratification_only")
            manifest = world.interactive.store.read(unit_row(world, key)["manifest_path"])
            block = manifest["project"]["repository_metadata"]["curated_selection"]
            self.assertEqual(block["catalog_handoff_verification"], "original_read_and_copy_compared")
            self.assertEqual(block["original_handoff"]["source"], "original_handoff_path")
            self.assertEqual(block["original_handoff"]["sha256"], planned[key]["curated_selection"]["catalog_handoff_sha256"])
            ratification = block["original_handoff"]["ratification"]
            self.assertTrue(ratification["differs"])
            self.assertEqual(ratification["original"]["ratification.approval_id"], "appr-A")
            self.assertEqual(ratification["copy"]["ratification.approval_id"], world.approval["approval_id"])
            self.assertEqual(ratification["copy"]["ratification.campaign_id"], world.campaign["campaign_id"])
            applied = [item["detail"] for item in events(world, "curated_selection_applied") if item["unit_key"] == key][0]
            self.assertTrue(applied["handoff_ratification"]["differs"])
        for sent in world.interactive.curated_downloads:
            self.assertTrue(sent["original_handoff_path"].endswith("handoff.triaged.json"))
        _lines, text = exported(world)
        self.assertFalse(ABSOLUTE.search(text), "the decision ledger holds workspace-relative paths only")

    def test_without_the_kept_triaged_path_interactive_reads_the_records_catalog_handoff_input(self) -> None:
        """The second way Interactive verifies the copy: no original_handoff_path, the decision record's catalog_handoff
        input. The runner's choice of handoff.triaged.json is one of two equal answers."""
        docs = {"AN004454": te.build_good("ST002747")["AN004454"]}
        world, planned = triage_world(self.root, "ST002747", docs, record=True, ratified={"AN004454": ratified_a("AN004454")})
        world.run()
        sent = dict(world.interactive.curated_downloads[0])
        sent.pop("original_handoff_path")
        sent["handoff_path"] = unit_row(world, "AN004454")["handoff_path"]
        provenance = world.interactive._apply_curated(sent)
        self.assertEqual(provenance["original_handoff"]["source"], "decision_record_input")
        self.assertTrue(provenance["original_handoff"]["ratification"]["differs"])
        self.assertTrue(planned)

    def test_a_triaged_proposal_with_another_assignment_is_held_and_interactive_would_refuse_it(self) -> None:
        docs = {"AN004454": te.build_good("ST002747")["AN004454"]}
        world, _planned = triage_world(self.root, "ST002747", docs, record=True, ratified={"AN004454": ratified_a("AN004454")},
                                       proposal_changes={"AN004454": {"assignments": [
                                           {"sample_id": "nobody", "class_label": "B", "values": {}}]}})
        with world.open() as book:
            world.runner(book).run(max_iterations=80)
        self.assertEqual(unit_row(world, "AN004454")["state"], "contract_held")
        checked = events(world, "triage_checked")[0]["detail"]
        self.assertEqual((checked["reason"], checked["differing_keys"]), ("triage_handoff_changed", ["class_proposal"]))
        self.assertEqual(world.interactive.download_starts, [])
        # Interactive, given the copy the runner made and the triaged bytes, refuses it for the same key.
        copy_path = world.directory / "handoffs" / "AN004454" / "handoff.json"
        with self.assertRaises(ia.CuratedSelectionError) as raised:
            ia.verify_handoff_copy(self.root / "triage" / "ST002747" / "inputs" / "AN004454" / "handoff.json",
                                   json.loads(copy_path.read_text(encoding="utf-8")))
        self.assertIn("class_proposal", str(raised.exception))

    def test_a_triage_made_before_any_ratification_is_held(self) -> None:
        docs = {"AN004454": te.build_good("ST002747")["AN004454"]}
        world, _planned = triage_world(self.root, "ST002747", docs, record=True, ratified={"AN004454": None})
        with world.open() as book:
            world.runner(book).run(max_iterations=80)
        self.assertEqual(unit_row(world, "AN004454")["state"], "contract_held")
        checked = events(world, "triage_checked")[0]["detail"]
        self.assertEqual(checked["differing_keys"], ["class_proposal"])


@unittest.skipUnless(REAL, "the pinned Interactive predates 0.5.69's curated selection (set MSDIAL_INTERACTIVE_ROOT)")
class WorkspaceNameAgainstInteractiveTests(unittest.TestCase):
    """curated.workspace_name_problem (the plan's check) refuses exactly what Interactive's validate_workspace_unit_key
    refuses (WS0 low, 2026-11-17)."""

    def test_the_two_agree(self) -> None:
        names = ("AN1", "AN1-pos", "AN1@stage3c", "AN1.r2", "AN1+x", "AN1_reopen", "AN10", "BN1", "x-AN1", "AN1/..",
                 "AN1\\x", "AN1..x", "AN1.", "AN1 x", "", "AN1@" + "x" * 123, "AN1@" + "x" * 124, "AN1@pilot.",
                 "AN1@a..b", "AN1@a.b", "AN1-pos@c-2")
        for name in names:
            with self.subTest(name=name):
                try:
                    ia_rr.validate_workspace_unit_key(name, "AN1")
                    accepted = True
                except ValueError:
                    accepted = False
                self.assertEqual(curated.workspace_name_problem(name, "AN1") == "", accepted)


class ReplayLevelTests(unittest.TestCase):
    """WS0 decision 9: the replay reads the release bundle (release projections), compares at the inputs level exactly
    the hashes the recorded projection schema names, and says not_evaluable, with why, where a level needs a Console
    run. Needs no Interactive."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def bundle(self) -> tuple[Path, Path, Path]:
        """(release bundle, replay workspace root, the replayed unit workspace) for one unit AN1 of ST1."""
        campaign = self.root / "analysis" / "_campaigns" / "c1"
        triage_dir = campaign / "triage" / "ST1"
        triage_dir.mkdir(parents=True)
        triage = {"schema": "msdial-unit-triage.v1", "unit_id": "AN1", "accession": "ST1",
                  "rows": [{"row_index": 0, "sample_id": "S1", "member_path": "study/POS/S1.mzML", "archive": "ST1.zip"}]}
        path = triage_dir / "AN1.json"
        path.write_text(json.dumps(triage), encoding="utf-8")
        manifest = {"campaign_id": "c1", "triage": {"directory": str(campaign / "triage")}, "units": [{
            "unit_id": "AN1", "unit_key": "AN1", "repository": REPOSITORY, "accession": "ST1", "workspace_name": "AN1",
            "curated_selection": {"triage_relative_path": "ST1/AN1.json", "triage_path": str(path),
                                  "triage_sha256": _sha(path.read_bytes()), "selection_sha256": curated.selection_sha256(triage, {}),
                                  "analysis_inputs_sha256": _sha(curated.canonical(curated.analysis_inputs(triage)))}}]}
        data = curated.canonical_json(manifest)
        (campaign / "campaign-manifest.json").write_bytes(data)
        unit_workspace = self.root / "analysis" / REPOSITORY / "ST1" / "AN1"
        (unit_workspace / "provenance").mkdir(parents=True)
        recorded = {"raw_directory": str(unit_workspace / "raw"), "analysis_csv": {"canonical_sha256": "a" * 64},
                    "replay_projection": {"schema": "msdial-replay-projection.v1"}, "replay_projection_sha256": "b" * 64,
                    "input_directory": str(unit_workspace / "raw" / "data")}
        (unit_workspace / "provenance" / "run-manifest.json").write_text(json.dumps(recorded), encoding="utf-8")
        release = self.root / "release"
        curated.write_release(campaign_dir=campaign, out=release, workspace_root=self.root / "analysis",
                              approved_digest="sha256:" + _sha(data),
                              run_manifests=[("AN1", unit_workspace / "provenance" / "run-manifest.json", unit_workspace)])
        replay_root = self.root / "replay"
        replayed = replay_root / REPOSITORY / "ST1" / "AN1"
        member = replayed / "raw" / "data" / "study" / "POS" / "S1.mzML"
        member.parent.mkdir(parents=True)
        member.write_bytes(b"synthetic")
        (replayed / "provenance").mkdir(parents=True)
        return release, replay_root, replayed

    def test_the_release_projection_holds_no_absolute_path_and_the_index_names_both_sha256(self) -> None:
        release, _root, _replayed = self.bundle()
        index = json.loads((release / "release-index.json").read_text(encoding="utf-8"))
        names = {item["name"]: item for item in index["manifests"]}
        self.assertEqual(set(names), {"campaign-manifest.json", "units/AN1/run-manifest.json"})
        released = (release / "units" / "AN1" / "run-manifest.release.json").read_text(encoding="utf-8")
        self.assertFalse(ABSOLUTE.search(released))
        self.assertEqual(json.loads(released)["raw_directory"], "raw")
        self.assertEqual(names["units/AN1/run-manifest.json"]["release_sha256"], _sha(released.encode("utf-8")))
        with self.assertRaises(curated.TriageError):
            curated.write_release(campaign_dir=self.root / "analysis" / "_campaigns" / "c1", out=release,
                                  workspace_root=self.root / "analysis", approved_digest="sha256:0")

    def test_inputs_compare_only_what_the_projection_schema_names(self) -> None:
        import test_campaign_curated_selection as module

        release, replay_root, replayed = self.bundle()
        manifest = {"raw_directory": str(replayed / "raw"), "analysis_csv": {"canonical_sha256": "a" * 64},
                    "parameter_file": {"canonical_sha256": "c" * 64}}
        (replayed / "provenance" / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = module.replay_cli.replay(release, replay_root, "inputs", out=self.root / "r1.json")
        row = [item for item in report["units"] if item["unit_key"] == "AN1"][0]
        checks = {item["check"]: item for item in row["checks"]}
        self.assertEqual(row["projection_schema"], "msdial-replay-projection.v1")
        self.assertEqual(checks["analysis_csv.canonical_sha256 (msdial-replay-projection.v1)"]["result"], module.replay_cli.EQUAL)
        self.assertFalse([name for name in checks if "parameter_file" in name], "v1 names no parameter-file hash")
        projection = checks["replay_projection_sha256 (msdial-replay-projection.v1)"]
        self.assertEqual(projection["result"], module.replay_cli.NOT_EVALUABLE)
        self.assertIn("after a Console run", projection["why"])
        manifest["replay_projection_sha256"] = "b" * 64
        (replayed / "provenance" / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = module.replay_cli.replay(release, replay_root, "inputs", out=self.root / "r2.json")
        row = [item for item in report["units"] if item["unit_key"] == "AN1"][0]
        self.assertEqual({item["check"]: item["result"] for item in row["checks"]}["replay_projection_sha256 (msdial-replay-projection.v1)"],
                         module.replay_cli.EQUAL)
        self.assertEqual({item["check"]: item["result"] for item in row["checks"]}["members"], module.replay_cli.EQUAL)

    def test_outputs_need_a_console_run_and_compare_after_the_path_rule(self) -> None:
        import test_campaign_curated_selection as module

        release, replay_root, replayed = self.bundle()
        recorded_raw = str(self.root / "analysis" / REPOSITORY / "ST1" / "AN1" / "raw")
        line = f"MTD\tms_run[1]-location\t{recorded_raw}\\data\\study\\POS\\S1.mzML\n"
        expected = module.replay_cli._sha(f"MTD\tms_run[1]-location\tdata/study/POS/S1.mzML\n".encode("utf-8"))
        (release / "units" / "AN1" / "output-checksums.tsv").write_text(
            f"path\tsha256\tcanonical_sha256\nresult.mztab\t{_sha(line.encode('utf-8'))}\t{expected}\n", encoding="utf-8")
        report = module.replay_cli.replay(release, replay_root, "outputs", out=self.root / "r3.json")
        row = [item for item in report["units"] if item["unit_key"] == "AN1"][0]
        outputs = [item for item in row["checks"] if item["check"] == "outputs"][0]
        self.assertIn("needs a Console run", outputs["why"])
        (replayed / "output").mkdir()
        (replayed / "output" / "result.mztab").write_text(
            f"MTD\tms_run[1]-location\t{replayed / 'raw'}\\data\\study\\POS\\S1.mzML\n", encoding="utf-8")
        (replayed / "provenance" / "run-manifest.json").write_text(json.dumps({"raw_directory": str(replayed / "raw")}),
                                                                   encoding="utf-8")
        report = module.replay_cli.replay(release, replay_root, "outputs", out=self.root / "r4.json")
        row = [item for item in report["units"] if item["unit_key"] == "AN1"][0]
        self.assertEqual({item["check"]: item["result"] for item in row["checks"]}["output:result.mztab"], module.replay_cli.EQUAL)


@unittest.skipUnless(REAL, "the pinned Interactive predates 0.5.69's curated selection (set MSDIAL_INTERACTIVE_ROOT)")
class PortAgainstInteractiveTests(unittest.TestCase):
    """The real port against the real tools, with the backend helper replaced so nothing leaves the process (and the
    port pointed nowhere): no backend is reached."""

    def setUp(self) -> None:
        patcher = mock.patch.object(ia_tools, "_request_json",
                                    side_effect=ia_tools.MsdialRequestError("Could not connect (the test reaches no backend)"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_real_tool_refuses_a_single_manifest_as_a_validation_error_not_unsupported(self) -> None:
        port = ports.InteractivePort(port=1)
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "run-manifest.json"
            manifest.write_text(json.dumps({"status": "preflight_passed", "project": {"analysis_unit_id": "AN1"}}), encoding="utf-8")
            result = port.cross_unit_check(manifest_paths=[str(manifest)])
        self.assertIs(result.get("ok"), False)
        self.assertNotEqual(result.get("reason"), "unsupported")
        self.assertIn("two or more", str(result.get("detail")))

    def test_the_download_takes_every_curated_argument_the_runner_sends(self) -> None:
        port = ports.InteractivePort(port=1)
        with tempfile.TemporaryDirectory() as directory:
            result = port.download(
                repository=REPOSITORY, accession="ST000001", workspace_root=directory, maximum_gb=1, raw_retention_policy="keep",
                handoff_path=str(Path(directory) / "none.json"), analysis_purpose="p", authorization_path="",
                curated_selection_path=str(Path(directory) / "none-triage.json"), curated_selection_child_key="",
                original_handoff_sha256="0" * 64, original_handoff_path="", decision_record_path="",
                curated_selection_validator={"verdict": "pass"}, workspace_unit_key="")
        self.assertIs(result.get("ok"), False)
        self.assertNotEqual(result.get("reason"), "unsupported", result)

    def test_interactives_refusal_codes_are_all_terminal_for_the_runner(self) -> None:
        for code in sorted(ia.CURATED_SELECTION_REFUSALS | ia.CROSS_UNIT_REFUSALS):
            if code == ia.TRIAGE_HEADER_DISAGREEMENT:
                continue  # a disposition hold that waits for a person, never a refusal before download
            with self.subTest(code=code):
                failure = {"ok": False, "reason": code, "reason_code": code, "retryable": False, "detail": code}
                self.assertEqual(policy.not_retryable(failure)["reason_code"], code)
                self.assertIn(code, policy.NOT_RETRYABLE_REFUSALS)


if __name__ == "__main__":
    unittest.main()
