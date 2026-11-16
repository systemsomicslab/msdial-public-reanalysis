# LLM-assisted curation: transparency and replay

A language model (LLM) helps this campaign with data management and metadata curation. A paper built on
that work must not contain a black box. This document is the contract the build agents implement and the
source of the Methods text. Three principles carry it.

1. **The LLM writes files, never state.** Every LLM contribution ends as a frozen decision file. Nothing
   downstream reads a conversation, a prompt or a model; it reads files and their sha256.
2. **Everything after the frozen files is deterministic and replayable without any LLM.** Validators, gates,
   the plan, download, preflight, MS-DIAL and QA run from pinned code. A third party who holds the decision
   files recomputes the selection and the MS-DIAL inputs to the extent section 3 states for each level.
3. **LLM variability is measured, not assumed away.** A model run is not bit reproducible, so the triage
   itself is re-derived on a stratified sample and on the eval set, and agreement is reported.

## 1. Division of labour

| Who | Decides | Recorded as |
|---|---|---|
| LLM (skill driven) | file to unit assignment (`msdial-unit-triage`); interpretation of repository metadata (polarity, chromatography, acquisition, role, factors read from free text); Class proposal drafts; questions for the person | decision files (section 2), all with `standing: model_inference` |
| Deterministic code | schema and contract checks (`scripts/validate-triage.py`); coverage and cross-unit attribution gates (Interactive, rules P, C and D); manifest and digest (`scripts/campaign/plan.py`); download with checksum; header preflight and its reconciliation against the triage (polarity and acquisition mode family); analysis CSV; MS-DIAL Console; mzTab-M validation; QA | verdicts in the review log and the decision ledger (section 2.4), manifests, run manifests |
| Person | the six confirmation boundaries of `CLAUDE.md`; answers to `needs_person` questions and blocking flags; every disagreement between the LLM and header evidence; approval of the manifest digest, which is the only step that turns an inference into an accepted decision | `person_answer` entries, the ledger approval |

The LLM never calls a tool with `confirmed=true`, never writes to the Catalog and never records a person's
answer on its own. A person's answer never edits a frozen file; it produces a revision (section 2.4).

## 2. Decision records

### 2.1 Where they live

```text
<campaign dir>\triage\<accession>\
    <unit_id>.json              payload: msdial-unit-triage.v2 (one per unit, immutable once written)
    <unit_id>.record.json       envelope: msdial-llm-decision-record.v1 (written by the wrapper, not the model)
    <unit_id>.r<N>.json         a revision, if any (with its own .record.json)
    inputs\                     the exact bytes the LLM read (handoff copies, mwTab text, archive listing)
    inputs\<unit_id>\prompt.md  the rendered prompt
    inputs\<unit_id>\calls\     one file per tool result, named <seq>.json
    review-log.jsonl            append only, hash chained, one per accession
<campaign dir>\decision-ledger.jsonl                   append only, hash chained, one per campaign (section 2.4)
<campaign dir>\decisions\<kind>\<unit_id>.record.json   other kinds (metadata_interpretation, class_proposal, question)
```

### 2.2 Payload: `msdial-unit-triage.v2`

v2 is v1 plus these changes:

- `confidence` (`high`, `medium`, `low`) on every `rows[]`, `added_members[]`, `excluded_rows[]` and
  `excluded_members[]` item;
- `rationale` (plain language, one or two sentences) on the same items and once at unit level;
- `person_answers` is no longer written into the payload. It stays readable in v1 files; in v2 answers are
  `person_answer` entries of the review log, so the payload hash never changes.

The evidence per decision is the existing `evidence: [{source, field, value}]` array (for example
`{"source": "catalog_row", "field": "Mode", "value": "C18 Negative"}`) with `basis: declared|inferred`. A
decision with `basis: inferred` and `confidence: low` cannot be `ready`; the validator makes it a hold.
`validate-triage.py` accepts v1 and v2 and reports v1 files as `rationale_not_recorded`.

For v2, `validate-triage.py <triage dir>` reads each accession's `review-log.jsonl` in place of
`person_answers`. A `ready` unit that carries a blocking flag (T-STATUS) passes only when the log holds a
`person_answer` whose `question_code` is that flag's code and whose `decision_id` is this payload or one it
supersedes. A revision that removes a blocking flag the superseded payload carried passes only when its
`cause` cites such a `person_answer` with the matching `question_code`.

### 2.3 Envelope: `msdial-llm-decision-record.v1` (`schemas/msdial-llm-decision-record.v1.json`)

The model writes only the payload. The envelope is written by the wrapper that launched the agent
(`scripts/run-triage-agent.py`), from the harness's own transcript after the session ends, so `tool_calls`
is never a list the model reports about itself.

```json
{
  "schema": "msdial-llm-decision-record.v1",
  "decision_id": "<sha256 of the payload bytes>",
  "kind": "unit_triage | metadata_interpretation | class_proposal | question",
  "accession": "ST002798", "unit_id": "AN004552",
  "payload": {"path": "AN004552.json", "schema": "msdial-unit-triage.v2", "sha256": "..."},
  "supersedes": null,
  "inputs": [
    {"role": "catalog_handoff", "path": "inputs/AN004552/handoff.json", "sha256": "...", "bytes": 0,
     "source": "msdial_catalog_reanalysis_handoff", "catalog_version": "0.6.x", "retrieved_at": "..."},
    {"role": "catalog_samples", "path": "inputs/AN004552/samples.json", "sha256": "...", "bytes": 0},
    {"role": "archive_listing", "path": "inputs/ST002798_rawdata.members.tsv", "sha256": "...", "bytes": 0,
     "retrieval": {"method": "remote_central_directory", "tool": "msdial_list_remote_archive",
                   "url": "...", "archive_bytes": 0, "etag": "...", "last_modified": "...", "range_bytes_read": 0}},
    {"role": "metadata_text", "path": "inputs/AN004552.mwtab.txt", "sha256": "...", "bytes": 0,
     "source_url": "https://www.metabolomicsworkbench.org/rest/study/analysis_id/AN004552/mwtab/txt"}
  ],
  "skill": {"name": "msdial-unit-triage", "version": "1.1.0", "path": ".claude/skills/msdial-unit-triage/SKILL.md",
            "sha256": "...", "repo_commit": "<40 hex>"},
  "prompt": {"template_path": "prompts/unit-triage-task.v1.md", "template_sha256": "...",
             "rendered_path": "inputs/AN004552/prompt.md", "rendered_sha256": "..."},
  "agent": {"harness": "claude-code", "harness_version": "...", "model_id": "claude-opus-5-5",
            "sampling": "harness default, not exposed", "session_id": "...",
            "context": {"configuration": "clean", "working_directory": "<empty directory holding only the skill>",
                        "context_files": [], "memory": "disabled", "hooks": [],
                        "mcp_servers": [{"name": "msdial-repository-catalog", "version": "..."}],
                        "harness_flags": ["..."]}},
  "started_at": "2026-11-16T09:12:00+09:00", "finished_at": "...",
  "tool_calls": [{"seq": 1, "tool": "msdial_catalog_get_analysis_unit", "arguments": {"unit_id": "..."},
                  "status": "ok", "result_path": "inputs/AN004552/calls/1.json", "result_sha256": "..."}],
  "transcript": {"sha256": "<sha256 of the released redacted bytes>", "released_path": "transcripts/AN004552.redacted.jsonl",
                 "unredacted_sha256": "<kept locally, not verifiable from the bundle>",
                 "redaction": "check-release-redaction.py v1"},
  "rationale": "Plain language summary of the unit decision."
}
```

Every input the LLM read is copied into `inputs\` and hashed, so a later change at the repository cannot
change what the decision rested on; so are the rendered prompt and every tool result. Tool arguments are
recorded with secrets removed.

**Context.** Triage runs in a clean harness configuration: a working directory outside every code checkout
that holds only the skill, no user or project `CLAUDE.md`, auto memory disabled, no hooks, and only the MCP
servers the skill allows. `agent.context` records that configuration, with every context file the harness
loaded and its sha256; the list is expected to be empty. The harness's own system prompt belongs to the
harness version and is identified by `harness_version`; it is not ours to release. A record whose
`agent.context.configuration` is not `clean`, or whose `context_files` is not empty, is a hold.

**What `validate-triage.py` recomputes**, refusing any mismatch: `decision_id` and `payload.sha256` from the
payload; `sha256` and `bytes` of every `inputs[]` item; `skill.sha256` from `SKILL.md` at `repo_commit`;
`prompt.template_sha256` and `prompt.rendered_sha256` from the stored files; every
`tool_calls[].result_sha256` from its `result_path`; `transcript.sha256` from the released redacted
transcript; the sha256 of each `context_files` item. It also refuses a tool call not on the skill's allowed
list, and a `tool_calls` list that differs from the tool calls the released transcript shows. It does not
recompute `transcript.unredacted_sha256` or `session_id`, which only the producing machine can check.

Other kinds use the same envelope: `metadata_interpretation` (field, source text sha256, value, evidence,
confidence, rationale), `class_proposal` (Catalog proposal id and digest, rationale; saving stays boundary
3) and `question` (text, unit, what it blocks).

### 2.4 Review log and decision ledger

Two append only, hash chained files record everything that happens after a decision file is written. Each
line is canonical JSON and carries `prev_sha256`, the sha256 of the previous line's bytes without its
newline (`null` for the first).

**`review-log.jsonl`** (one per accession, in the triage directory) records what happens to the decision
files before and at the approval. Each entry carries `seq`, `at`, `kind`, `decision_id`, `prev_sha256` and:

| kind | fields | written by |
|---|---|---|
| `validator_verdict` | `tool` (`validate-triage.py`), `code_commit`, `verdict` (pass, hold, error), `findings_sha256`, `findings` (check, level, message) | `validate-triage.py --emit-selection` |
| `person_answer` | `by` (name or the initials the person agreed to publish), `question_code`, `answer` (verbatim), `channel` (`conversation`) | only from what a named person said in the conversation, under the rule of `scripts/record-reading.py` |
| `revision` | `supersedes`, `new_decision_id`, `cause` (`{kind, seq}` of a `person_answer`, a ledger `header_reconciliation` or a `validator_verdict`) | the agent |
| `digest_approval` | `manifest_digest`, `approval_id`, `by` (copied from the ledger) | the runner |

**`decision-ledger.jsonl`** (one per campaign, in the campaign directory) is exported by
`campaign-runner.py decisions` from the campaign's events. Each line is `{seq, event_seq, at, kind,
unit_key, standing, detail, prev_sha256}`, where `standing` is one of `source_fact`, `model_inference`,
`deterministic_check`, `accepted_decision` and `person_answer`. The gate and header verdicts are recorded
here, not in the review log:

| ledger kind | standing | records |
|---|---|---|
| `triage_recorded` | model_inference | the triage and record sha256, rows kept, members added, rows dropped by reason, `selection_sha256` |
| `triage_validated` | deterministic_check | the validator's sha256 and verdict, `review_log_head_sha256` |
| `manifest_approved` | accepted_decision | `manifest_digest`, `approval_id`, `approved_by`, the selections it covers |
| `unit_reopened` | accepted_decision | the reason code and the evidence sha256 |
| `triage_checked` | deterministic_check | the runner's sha256 check of the triage against the manifest at `copy_handoff` |
| `curated_selection_applied` | accepted_decision | the `selection_sha256` the plan recorded, the one Interactive recorded, and whether they are equal |
| `header_reconciliation` | deterministic_check | Interactive's reconciliation verdict and its per file items (section 6) |
| `cross_unit_verdict` | deterministic_check | the cross-unit attribution gate, rule P, C or D, and the Interactive version |
| `terminal_refusal` | deterministic_check | a refusal that ends the unit (attribution coverage, `attribution_refused`, policy drift) |
| `person_answer` | person_answer | a person's answer that bears on a running unit |

`scripts/verify-decision-log.py <campaign dir>` walks every review log and the ledger. It refuses a broken
link; a `decision_id` without its payload; a revision whose `cause` names no entry of the stated kind; a
revision that removes a blocking flag without a `person_answer` of the matching `question_code` (section
2.2); a ledger `triage_recorded` whose triage sha256 matches no payload; and a `triage_validated` whose
`review_log_head_sha256` is not the hash of a line of that accession's review log.

### 2.5 Manifest and run manifest

The plan (`scripts/campaign/plan.py`) adds per unit: `triage_path`, `triage_sha256`, `triage_record_sha256`,
`review_log_head_sha256`, `rows_kept`, `rows_dropped`, `selection_sha256` (section 3.1). The digest a person
approves therefore covers the frozen decisions and every review entry before it. The runner refuses at
`copy_handoff` a triage whose sha256 differs from the manifest.

Interactive records in `provenance\run-manifest.json` under `repository_metadata.curated_selection`:
`triage_sha256`, `triage_record_sha256`, `catalog_handoff_sha256`, `manifest_digest`, `selection_sha256`,
rows kept and dropped with reasons, and per input `selected_by: curated_selection`. It also writes
`analysis_csv.canonical_sha256` and `replay_projection_sha256` (section 3.2). At preflight the runner
compares the `selection_sha256` Interactive recorded with the one in the manifest and holds the unit when
they differ (`curated_selection_applied` records both).

## 3. Replay without any LLM

A replay starts from the released bundle (section 5) and pinned code: the reanalysis repository commit, the
Interactive and Catalog versions and commits, the Console commit and binary sha256, and the raw metadata
extractor pin, all already in the manifest.

```text
<python> scripts/replay-campaign.py --bundle <release dir> --workspace-root <absolute path>
    --level selection | inputs | outputs   [--unit <unit key>] [--json]
```

Exit status 0 equal, 1 different, 2 not evaluable (never 0 for a check that could not run). It writes
`replay-report.json`, one row per unit with the expected and replayed value of every compared hash and the
schema each was taken under. The replay module imports no agent or model client; a test asserts this.

| Level | Needs | Recomputes | Equal when |
|---|---|---|---|
| `selection` | the bundle only; no network, no raw data | validator verdicts; the selection (section 3.1) | `selection_sha256` and every validator verdict equal the recorded ones; the manifest digest recomputes |
| `inputs` | the raw archives, downloaded by the third party and checked against the listing's CRC32 and size | header preflight, reconciliation, analysis CSV, parameter file | every hash the recorded projection schema names is equal (section 3.2) |
| `outputs` | the pinned Console and the same libraries | MS-DIAL run and its finalisation | the canonical sha256 of each retained MS-DIAL text output is equal (section 3.3) |

### 3.1 The selection (`msdial-selection-canonical.v1`)

The selection is the triage's decision: every file the payload keeps, its `rows[]` and its
`added_members[]`, whether or not a given Interactive version admits added members (0.5.69 records them
under `added_members_not_admitted`; what was analysed is fixed by `analysis_csv.canonical_sha256`). The
definition is normative; the plan, the runner, Interactive and `replay-campaign.py` import or vendor this
function unchanged and give the same value for the same unit.

- **Fields.** `unit_id` is the Catalog unit id, bare, also for a child; `child_key` is the child's key or
  `null`; `member_path` is the triage's own, with `\` turned into `/` and leading and trailing `/` removed,
  case kept; `archive_name` is the triage item's `archive`, else the listing's; `sample_row_index` is the
  row's `row_index`, `null` for an added member.
- **Sources.** `crc32` and `size` come from the archive listing the decision record binds (its `inputs[]`
  item with `role: archive_listing`, columns `archive`, `path`, `size`, `crc32`, matched on the case folded
  member path), never from the triage and never from a download. Without such a listing, or without a
  match, both are `null`. A reader holding a listing whose sha256 differs from the record's refuses.
- **Children.** A child keeps the items whose member path is in its `member_paths`.
- **Order.** Sorted by `unit_id`, `child_key` (null as empty), `member_path`, `sample_row_index` (null as
  -1), then by the entry's canonical JSON.
- **Hash.** `selection_sha256` is the sha256 of the canonical JSON (sorted keys, no spaces, UTF-8, no ASCII
  escaping) of the sorted list.

```python
# msdial-selection-canonical.v1 reference implementation
import hashlib
import json


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _member(path):
    return str(path or "").replace("\\", "/").strip().strip("/")


def selection_entries(triage, listing, child=None):
    """listing: {case folded member path: {"archive", "crc32", "size"}} from the record's archive_listing input,
    {} when it binds none. child: one item of triage["children"], or None for the whole unit."""
    child_key = None if child is None else str(child["child_key"])
    allowed = None if child is None else {_member(p).casefold() for p in child.get("member_paths") or []}
    kept = [(item, item.get("row_index")) for item in triage.get("rows") or []]
    kept += [(item, None) for item in triage.get("added_members") or []]
    entries = []
    for item, row_index in kept:
        member = _member(item.get("member_path"))
        if allowed is not None and member.casefold() not in allowed:
            continue
        listed = (listing.get(member.casefold()) or {}) if member else {}
        crc32, size = listed.get("crc32"), listed.get("size")
        entries.append({
            "unit_id": str(triage["unit_id"]),
            "child_key": child_key,
            "member_path": member or None,
            "archive_name": item.get("archive") or listed.get("archive") or None,
            "crc32": None if crc32 in (None, "") else str(crc32).upper().zfill(8),
            "size": None if size in (None, "") else int(size),
            "sample_row_index": None if row_index is None else int(row_index),
        })
    return sorted(entries, key=lambda e: (e["unit_id"], e["child_key"] or "", e["member_path"] or "",
                                          -1 if e["sample_row_index"] is None else e["sample_row_index"],
                                          canonical_json(e)))


def selection_sha256(triage, listing, child=None):
    return hashlib.sha256(canonical_json(selection_entries(triage, listing, child))).hexdigest()
```

Worked example. The triage and the parsed listing:

```json selection-example
{
  "triage": {"unit_id": "AN000001",
             "rows": [{"row_index": 2, "member_path": "study\\POS\\S02.mzML", "archive": "ST000001_rawdata.zip"},
                      {"row_index": 1, "member_path": "study/POS/S01.mzML", "archive": "ST000001_rawdata.zip"}],
             "added_members": [{"member_path": "study/POS/QC01.mzML", "archive": "ST000001_rawdata.zip"}]},
  "listing": {"study/pos/s01.mzml": {"archive": "ST000001_rawdata.zip", "crc32": "abcdef01", "size": 1234},
              "study/pos/s02.mzml": {"archive": "ST000001_rawdata.zip", "crc32": "0BCDEF2", "size": 5678}}
}
```

give three entries in the order `QC01` (crc32 and size `null`, `sample_row_index` `null`), `S01`
(`ABCDEF01`, 1234, 1), `S02` (`00BCDEF2`, 5678, 2), and

```text selection-example-sha256
4c609902ea5315cf4ab6886cbfdd72ca45ff2892c172e4060822975b8ef10804
```

The same triage with no listing gives `e33f229c94650c9b06bb1fa48c9180923aa029ab38af41ad325e4d131138898f`.

### 3.2 The inputs level

- **`analysis_csv.canonical_sha256`** (`msdial-analysis-csv-canonical.v1`): the analysis CSV with every
  absolute path rewritten relative to the unit's raw directory (elsewhere in the unit workspace as
  `../<path>`, outside it as `<external>/<file name>`), forward slashes, LF line endings, UTF-8 without BOM.
- **`parameter_file.canonical_sha256`** (`msdial-parameter-file-canonical.v1`): the Console parameter file
  with paths rewritten by the same rule, every library path replaced by `<library:<file name>
  sha256=<sha256>>`, trailing whitespace removed, LF line endings, UTF-8 without BOM. Interactive records it
  in the run manifest from the next free patch version after 0.5.69.
- **`replay_projection_sha256`**: canonical JSON of a whitelist of the run manifest. Timestamps, job ids,
  host names and absolute paths are excluded by construction. The run manifest names the projection schema.
  - `msdial-replay-projection.v1` (Interactive 0.5.69): repository, accession, unit id,
    `catalog_handoff_sha256`, the `curated_selection` hashes, ion mode, acquisition mode, separation, inputs
    (member path, identity, sample id, acquisition type, header polarity), `analysis_csv.canonical_sha256`,
    the accepted minimum peak height with its diagnostic count and step, the Interactive version. It does not
    cover libraries, the Console identity or the parameter file.
  - `msdial-replay-projection.v2` (next Interactive patch version): v1 plus Class per input,
    `parameter_file.canonical_sha256`, libraries by file name and sha256, and the Console commit and binary
    sha256.

The `inputs` level compares exactly the hashes the recorded projection schema names: for v1,
`analysis_csv.canonical_sha256` and `replay_projection_sha256`; for v2, also
`parameter_file.canonical_sha256`. A hash the schema does not name is not a check that could not run, and
the report says which schema it compared under. A unit run under v1 that replays equal has the same file
list and settings in the projection, but a change of library or parameter file would not show; the Methods
say so for such units.

### 3.3 The outputs level

The Console output is bit deterministic for one build and one input set at the same paths, but the mzTab-M
the Console writes holds `ms_run[n]-location` and `database[n]-uri` as absolute `file://` paths
(`MztabFormatExport.cs`). Interactive's finalisation rewrites both for every repository run before
validation: each raw location relative to the unit workspace (`raw/...`), a private library's uri `null`
with a `custom` line naming it by file name and sha256 (`msdial_app/run_finalisation.py`). The outputs level
compares the finalised files after the path rule of section 3.2 is applied to any remaining absolute path.
The local only record of the original values that finalisation keeps is never released.

A unit run with a private library replays at `outputs` only with a file of the same sha256, which the bundle
names but does not contain (`not_evaluable: private_library`).

## 4. Re-derivation and variability

### 4.1 Re-running the triage

Released: `SKILL.md` at its commit, the prompt template, the rendered prompt, the tool results and the
`inputs\` bytes of every decision, and `agent.context`. What the model saw beyond these is the harness's own
system prompt, fixed by `harness_version`. A third party re-runs the triage in the same clean configuration
(section 2.3) with any capable agent harness, for example:

```text
<python> scripts/run-triage-agent.py --accession <accession> --triage-dir <empty dir> --model <model id>
<python> scripts/compare-triage.py <released triage dir> <re-derived triage dir> --json
```

The wrapper renders `prompts/unit-triage-task.v1.md` (the accession, directories and boundaries; the skill
states the rest), launches the harness in the clean configuration and writes the envelopes.
`compare-triage.py` needs no model. Within model runs in the laboratory use the same configuration, so they
share no memory the third party lacks.

### 4.2 Measuring agreement

- **Sample.** Stratify accessions by repository, by whether membership is declared (Catalog row attribute or
  mwTab Samples line) or inferred from names, by unit count (1, 2, 3 to 4, 5 or more) and by whether an
  archive listing was available. Draw 10% of triaged accessions, at least 30 and at least 3 per stratum
  (every accession of a smaller stratum), with a fixed seed recorded in `rederivation/sample.json`.
- **Runs.** Each sampled accession is re-triaged twice: a fresh session of the same model (within model) and
  a different model (between model). Model ids and dates are recorded in each envelope.
- **Per file.** Each raw-like archive member gets one label: the unit that keeps it, `excluded:<reason>` or
  `abstained`. Report raw agreement and Cohen's kappa per accession (median and range across accessions) and
  pooled over all files. Where both runs give one label to every file of an accession, kappa is undefined:
  that accession reports raw agreement only, is left out of the kappa median and range, and the count of
  such accessions is reported beside them.
- **Per unit.** Report agreement and kappa on `status`, the fraction of units with an identical kept set, and
  the median Jaccard index of kept sets.
- **After the gates.** Report the fraction of units whose approved selection (after validator, person queue
  and header reconciliation) is identical between runs. This figure bears on the results; the gap to raw
  agreement shows what the deterministic stages corrected.

### 4.3 Eval set accuracy and validator catch rate

The eval set is `tests/fixtures/triage-eval/` (eight Metabolomics Workbench accessions with ground truth from
raw headers, mzML source paths, elution order and mwTab). For every skill version and model, `tests/triage_eval.py`
reports per file correct, wrong and abstained, and per unit the expected pre-download status. The validator's
catch rate is the fraction of wrong assignments (against ground truth) that `validate-triage.py` raises as
error or hold, reported for real LLM output and for the synthetic mutations the test suite builds. A unit whose
header reconciliation establishes ground truth in production may be added to the eval set; the set carries an
`eval_set_version`, and accuracy is always reported with the version it was measured on.

## 5. Disclosure in the paper

### 5.1 Methods paragraph (template)

> **Assignment of raw files to analysis units.** Raw files were assigned to analysis units before download by
> a large language model agent ({model_id}) following a written procedure (msdial-unit-triage {version},
> Supplementary Data {n}). The agent ran without user or project memory and saw only the procedure, a
> task prompt and the inputs it retrieved. For each unit, the agent read the repository's sample rows and
> attributes, the mwTab sample and factor lines, and the member listing of the study archive read from its
> zip central directory, and wrote one decision file per unit stating the files kept and excluded, the source
> field and value supporting each choice, a confidence level and a rationale. Every input and tool result was
> stored with its sha256 together with the procedure, the prompt, the model identifier and the tool calls
> recorded by the harness. Decision files were checked by a deterministic validator for coverage of every
> sample row, files claimed by two units, byte identical content across units and consistency of polarity,
> chromatography and acquisition mode with name tokens and declared settings. Decision files the validator
> rejected were regenerated ({n_redo} regenerations over {n_units_redo} units). Units the validator held for
> a question, and every disagreement in polarity or acquisition mode between a decision and the raw file
> headers after download, were resolved by a person ({n_queue} of {n_units} units). A unit entered analysis
> only after a person approved a manifest digest covering its decision files. Agreement between independent
> triages was {kappa_file} (Cohen's kappa, per file; {n_kappa_undefined} accessions with a single label
> reported as raw agreement) on a stratified sample of {n_sample} accessions, and accuracy on an evaluation
> set of {n_eval} accessions with known assignment was {acc} (Figure {x}). All steps after the decision files
> used pinned code; without the language model, the released decision files reproduced the file selection of
> {n_selection_equal} of {n_units} units and the MS-DIAL input file lists of {n_inputs_equal} units
> (Supplementary Table {t}).

Fill the braces from `replay-report.json`, `rederivation/agreement.json`, `supplementary/llm-summary.json`
and `tests/triage_eval.py`.

### 5.2 Supplementary table: `supplementary/llm-decisions.tsv`

Written by `scripts/export-llm-decisions.py <campaign dir>`, one row per decision (revisions as their own rows):

`decision_id, kind, accession, unit_id, supersedes, revision_cause, model_id, agent_context, skill,
skill_version, skill_sha256, prompt_template_sha256, inputs_sha256 (sha256 of the sorted input hashes),
decided_at, n_rows_kept, n_rows_excluded, n_members_added, n_declared, n_inferred, n_low_confidence, status,
validator_verdict, validator_commit, gate_verdicts, n_header_disagreements, person_review (none, answered,
revised), reviewer, approved_manifest_digest, final_state (accepted, revised, excluded, withdrawn),
rederivation_sampled, rederivation_agreement, replay_selection_equal, replay_inputs_equal,
replay_projection_schema`

### 5.3 What is released (Zenodo, with the paper)

Released: the skill, prompt templates, schemas, validators, the wrapper and replay scripts at their commits;
the eval set with its versions; every decision file, envelope, `inputs\` directory (rendered prompts and tool
results included), review log and the decision ledger; the release projection of every campaign and run
manifest; `replay-report.json`; the re-derivation sample and agreement; the redacted transcripts of clean
configuration runs.

**Release projection.** A manifest is released as `<name>.release.json`: canonical JSON with every absolute
path under the workspace root made relative to the unit directory, any other path reduced to
`<external>/<file name>`, and every library named by file name and sha256. `release-index.json` lists, per
manifest, the sha256 of the original (checkable only on the producing machine) and of the projection. The
replay compares `replay_projection_sha256` and the canonical hashes, which do not depend on paths, so the
projection loses nothing the replay needs.

Not released: private library content (VS20/VS21 MSP, laboratory LBM2), named only by file name and sha256,
never by any path; raw data, referenced by accession, archive name, size and CRC32; finalisation's local
only record of original mzTab-M values; transcripts of runs outside the clean configuration.
`scripts/check-release-redaction.py <bundle>` runs over every file of the bundle, redistributed MS-DIAL
outputs included, and refuses a private library name with a path; a drive letter path; a UNC path
(`\\host\share`); a user profile path; and in a transcript, any block the harness inserts from a context
file or memory.

## 6. Failure handling

- **LLM against header evidence.** After download, Interactive reconciles each inspected file's raw header
  against the triage's kept row: polarity, and acquisition mode family (DDA, the DIA family of DIA, SWATH and
  AIF, MS1 against full scan). MS levels, DIA window signature and run end time are not compared. Each per
  file item is `{file, member_path, row_index, basis, outcome, ion_mode: {triage, header, result},
  acquisition_mode: {triage, header, result}, result, reasons}`, each result `agree`, `disagree`,
  `unresolved` or `not_compared`. A file in no kept row (`not_in_selection`), or an unread header for an
  inferred row (`header_unread_for_inferred_row`), is a disagreement too. A disagreement holds the unit with
  reason `triage_header_disagreement` and the ledger's `header_reconciliation` entry copies the items.
  Neither side wins silently: the person accepts the header, accepts the triage or excludes the file, and a
  `revision` citing that entry is logged when the selection changes.
- **Validator error.** The triage is regenerated as `<unit_id>.r<N>.json` with its own envelope; the revision
  entry cites the `validator_verdict` as its cause. The errored payload stays in the bundle with its verdict,
  and the regeneration count per unit is reported.
- **Gate refusal** (rules P, C, D). The unit ends `attribution_refused`, is not retried, and its done outputs,
  if any, are withdrawn from redistribution.
- **Counts reported.** `scripts/export-llm-decisions.py --summary` writes `supplementary/llm-summary.json`:
  units triaged; units by status; files kept, excluded and abstained; validator errors and holds by check;
  regenerations after a validator error, and the units they affected; person queue size by question code;
  header disagreements by field (`ion_mode`, `acquisition_mode`, `not_in_selection`,
  `header_unread_for_inferred_row`) and by resolution; revisions by cause; units withdrawn. The Methods and
  Figure {x} cite these numbers only.

## 7. Build order

Schemas and skill 1.1.0; `run-triage-agent.py` (clean configuration, envelope from the harness transcript);
`validate-triage.py` (v2, envelope hashes, review log answers, `--emit-selection`, log append);
`verify-decision-log.py`, `compare-triage.py`, `export-llm-decisions.py` and `check-release-redaction.py`;
the plan and runner fields with the section 3.1 function and the preflight comparison of `selection_sha256`;
Interactive `curated_selection` with the section 3.1 function and the listing as its crc32 and size source,
then `parameter_file.canonical_sha256` and projection v2 (next free patch versions); `replay-campaign.py`
with a synthetic fixture test per level.
