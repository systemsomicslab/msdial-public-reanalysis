# LLM-assisted curation: transparency and replay

A language model (LLM) helps this campaign with data management and metadata curation. A paper built on
that work must not contain a black box. This document is the contract the build agents implement and the
source of the Methods text. Three principles carry it.

1. **The LLM writes files, never state.** Every LLM contribution ends as a frozen decision file. Nothing
   downstream reads a conversation, a prompt or a model; it reads files and their sha256.
2. **Everything after the frozen files is deterministic and replayable without any LLM.** Validators, gates,
   the plan, download, preflight, MS-DIAL and QA run from pinned code. A third party who holds the decision
   files gets identical MS-DIAL inputs.
3. **LLM variability is measured, not assumed away.** A model run is not bit reproducible, so the triage
   itself is re-derived on a stratified sample and on the eval set, and agreement is reported.

## 1. Division of labour

| Who | Decides | Recorded as |
|---|---|---|
| LLM (skill driven) | file to unit assignment (`msdial-unit-triage`); interpretation of repository metadata (polarity, chromatography, acquisition, role, factors read from free text); Class proposal drafts; questions for the person | decision files (section 2), all with `standing: model_inference` |
| Deterministic code | schema and contract checks (`scripts/validate-triage.py`); coverage and cross-unit attribution gates (Interactive, rules P, C and D); manifest and digest (`scripts/campaign/plan.py`); download with checksum; header preflight and its reconciliation against the triage; analysis CSV; MS-DIAL Console; mzTab-M validation; QA | verdict entries in the review log, manifests, run manifests |
| Person | the six confirmation boundaries of `CLAUDE.md`; answers to `needs_person` questions; every disagreement between the LLM and header evidence; approval of the manifest digest, which is the only step that turns an inference into an accepted decision | person entries in the review log, the ledger approval |

The LLM never calls a tool with `confirmed=true`, never writes to the Catalog and never records a person's
answer on its own. A person's answer never edits a frozen file; it produces a revision (section 2.4).

## 2. Decision records

### 2.1 Where they live

```text
<campaign dir>\triage\<accession>\
    <unit_id>.json              payload: msdial-unit-triage.v2 (one per unit, immutable once written)
    <unit_id>.record.json       envelope: msdial-llm-decision-record.v1
    <unit_id>.r2.json           a revision, if any (with its own .record.json)
    inputs\                     the exact bytes the LLM read (handoff copies, mwTab text, archive listing)
    review-log.jsonl            append only, hash chained, one per accession
<campaign dir>\decisions\<kind>\<unit_id>.record.json   other kinds (metadata_interpretation, class_proposal, question)
```

### 2.2 Payload: `msdial-unit-triage.v2`

v2 is v1 plus three fields, added without removing any:

- `confidence` (`high`, `medium`, `low`) on every `rows[]`, `added_members[]`, `excluded_rows[]` and
  `excluded_members[]` item;
- `rationale` (plain language, one or two sentences) on the same items and once at unit level;
- `person_answers` is no longer written into the payload. It stays readable in v1 files; in v2 answers go to
  the review log so the payload hash never changes.

The evidence per decision is the existing `evidence: [{source, field, value}]` array (for example
`{"source": "catalog_row", "field": "Mode", "value": "C18 Negative"}`) with `basis: declared|inferred`. A
decision with `basis: inferred` and `confidence: low` cannot be `ready`; the validator makes it a hold.
`validate-triage.py` accepts v1 and v2 and reports v1 files as `rationale_not_recorded`.

### 2.3 Envelope: `msdial-llm-decision-record.v1` (`schemas/msdial-llm-decision-record.v1.json`)

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
    {"role": "catalog_samples", "path": "inputs/AN004552/samples.json", "sha256": "..."},
    {"role": "archive_listing", "path": "inputs/ST002798_rawdata.members.tsv", "sha256": "...",
     "retrieval": {"method": "remote_central_directory", "tool": "msdial_list_remote_archive",
                   "url": "...", "archive_bytes": 0, "etag": "...", "last_modified": "...", "range_bytes_read": 0}},
    {"role": "metadata_text", "path": "inputs/AN004552.mwtab.txt", "sha256": "...",
     "source_url": "https://www.metabolomicsworkbench.org/rest/study/analysis_id/AN004552/mwtab/txt"}
  ],
  "skill": {"name": "msdial-unit-triage", "version": "1.0.0", "path": ".claude/skills/msdial-unit-triage/SKILL.md",
            "sha256": "...", "repo_commit": "<40 hex>"},
  "prompt": {"template_path": "prompts/unit-triage-task.v1.md", "template_sha256": "...", "rendered_sha256": "..."},
  "agent": {"harness": "claude-code", "harness_version": "...", "model_id": "claude-opus-5-5",
            "sampling": "harness default, not exposed", "session_id": "..."},
  "started_at": "2026-11-16T09:12:00+09:00", "finished_at": "...",
  "tool_calls": [{"seq": 1, "tool": "msdial_catalog_get_analysis_unit", "arguments": {"unit_id": "..."},
                  "status": "ok", "result_sha256": "..."}],
  "transcript": {"sha256": "...", "released_path": "transcripts/AN004552.redacted.jsonl", "redaction": "check-release-redaction.py v1"},
  "rationale": "Plain language summary of the unit decision."
}
```

Every input the LLM read is copied into `inputs\` and hashed, so a later change at the repository cannot
change what the decision rested on. Tool arguments are recorded with secrets removed, results by sha256. A decision whose tool calls include one not on the skill's allowed list is an error.
`scripts/validate-triage.py` recomputes every sha256 in the envelope and refuses a mismatch.

Other kinds use the same envelope: `metadata_interpretation` (field, source text sha256, value, evidence,
confidence, rationale), `class_proposal` (Catalog proposal id and digest, rationale; saving stays boundary
3) and `question` (text, unit, what it blocks).

### 2.4 Review log: `review-log.jsonl`

One JSON object per line, never rewritten. Each entry carries `seq`, `at`, `kind`, `decision_id`,
`prev_sha256` (sha256 of the previous line's bytes; `null` for the first) and kind specific fields:

| kind | fields |
|---|---|
| `validator_verdict` | `tool` (`validate-triage.py`), `code_commit`, `verdict` (pass, hold, error), `findings_sha256`, `findings` (check, level, message) |
| `gate_verdict` | `gate` (`attribution_coverage`, `cross_unit_attribution`), `rule` (P, C, D), `interactive_version`, `verdict` |
| `person_answer` | `by` (name or the initials the person agreed to publish), `question_code`, `answer` (verbatim), `channel` (`conversation`) |
| `header_reconciliation` | `member_path`, `triage_value`, `header_value`, `field` (ion_mode, acquisition_mode, ms_levels, dia_windows), `result` (agree, disagree) |
| `revision` | `supersedes`, `new_decision_id`, `cause` (person_answer seq or reconciliation seq) |
| `digest_approval` | `manifest_digest`, `approval_id`, `by` (copied from the ledger) |

`scripts/verify-decision-log.py <triage dir>` walks the chain and refuses a broken link, a decision id
without its payload, or a revision whose cause is missing. An agent may append `validator_verdict`,
`gate_verdict`, `header_reconciliation` and `revision` entries; `person_answer` is appended only from what a
named person said in the conversation, under the same rule as `scripts/record-reading.py`.

### 2.5 Manifest and run manifest

The plan (`scripts/campaign/plan.py`) adds per unit: `triage_path`, `triage_sha256`, `triage_record_sha256`,
`review_log_head_sha256`, `rows_kept`, `rows_dropped`, `selection_sha256` (section 3). The digest a person
approves therefore covers the frozen decisions and every review entry before it. The runner refuses at
`copy_handoff` a triage whose sha256 differs from the manifest.

Interactive records in `provenance\run-manifest.json` under `repository_metadata.curated_selection`:
`triage_sha256`, `triage_record_sha256`, `catalog_handoff_sha256`, `manifest_digest`, `selection_sha256`,
rows kept and dropped with reasons, and per input `selected_by: curated_selection`. It also writes
`analysis_csv.canonical_sha256` and `replay_projection_sha256` (section 3).

## 3. Replay without any LLM

A replay starts from the released bundle (section 5) and pinned code: the reanalysis repository commit, the
Interactive and Catalog versions and commits, the Console commit and binary sha256, and the raw metadata
extractor pin, all already in the manifest.

```text
<python> scripts/replay-campaign.py --bundle <release dir> --workspace-root <absolute path>
    --level selection | inputs | outputs   [--unit <unit key>] [--json]
```

Exit status 0 equal, 1 different, 2 not evaluable (never 0 for a check that could not run). It writes
`replay-report.json`, one row per unit with the expected and replayed value of every compared hash. The
replay module imports no agent or model client; a test asserts this.

| Level | Needs | Recomputes | Equal when |
|---|---|---|---|
| `selection` | the bundle only; no network, no raw data | validator verdicts; the selection, as canonical JSON of `[{unit_id, archive_sha256 or archive_name, member_path, crc32, size, sample_row_index, child_key}]` sorted | `selection_sha256` and every validator verdict equal the recorded ones; the manifest digest recomputes |
| `inputs` | the raw archives, downloaded by the third party and checked against the listing's CRC32 and size | header preflight, reconciliation, analysis CSV, parameter file | `analysis_csv.canonical_sha256`, `parameter_file_sha256` and `replay_projection_sha256` equal |
| `outputs` | the pinned Console and the same libraries | MS-DIAL run | sha256 of each MS-DIAL text output equals (the Console output is bit deterministic for one build and one input set) |

Canonical forms, because absolute paths and timestamps differ between machines:

- **`analysis_csv.canonical_sha256`**: the analysis CSV with every file path rewritten relative to the
  unit's raw directory, forward slashes, LF line endings, UTF-8 without BOM.
- **`replay_projection_sha256`**: canonical JSON (sorted keys, no spaces, UTF-8) of a whitelist of the run
  manifest: accession, unit id, `catalog_handoff_sha256`, `curated_selection` hashes, inputs (member path,
  CRC32, size, sample id, Class, acquisition type, header polarity), ion mode, acquisition mode, accepted
  minimum peak height with its diagnostic count, parameter file canonical sha256, libraries by name and
  sha256, Console commit and binary sha256, Interactive version. Timestamps, job ids, host names and
  absolute paths are excluded by construction.

A unit run with a private library replays to equality at `selection` and `inputs`; at `outputs` only with
a file of the same sha256, which the bundle names but does not contain (`not_evaluable: private_library`).

## 4. Re-derivation and variability

### 4.1 Re-running the triage

Everything the LLM saw is released: `SKILL.md` at its commit, the prompt template, and the `inputs\` bytes of
every decision. A third party re-runs it with any capable agent harness, for example:

```text
claude -p "$(cat prompts/unit-triage-task.v1.md)" --model <model id>    # task names <accession> and an empty triage dir
<python> scripts/compare-triage.py <released triage dir> <re-derived triage dir> --json
```

The prompt template names the accession, directories and boundaries; the skill states the rest.
`compare-triage.py` needs no model.

### 4.2 Measuring agreement

- **Sample.** Stratify accessions by repository, by whether membership is declared (Catalog row attribute or
  mwTab Samples line) or inferred from names, by unit count (2, 3 to 4, 5 or more) and by whether an archive
  listing was available. Draw 10% of triaged accessions, at least 30 and at least 3 per stratum, with a
  fixed seed recorded in `rederivation/sample.json`.
- **Runs.** Each sampled accession is re-triaged twice: a fresh session of the same model (within model) and
  a different model (between model). Model ids and dates are recorded in each envelope.
- **Per file.** Each raw-like archive member gets one label: the unit that keeps it, `excluded:<reason>` or
  `abstained`. Report raw agreement and Cohen's kappa per accession (median and range across accessions) and
  pooled over all files.
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
> Supplementary Data {n}). For each unit, the agent read the repository's sample rows and attributes, the
> mwTab sample and factor lines, and the member listing of the study archive read from its zip central
> directory, and wrote one decision file per unit stating the files kept and excluded, the source field and
> value supporting each choice, a confidence level and a rationale. Every input the agent read was stored with
> its sha256 together with the procedure, the model identifier and the tool calls made. Decision files were
> checked by a deterministic validator for coverage of every sample row, files claimed by two units, byte
> identical content across units and consistency of polarity, chromatography and acquisition mode with name
> tokens and declared settings. Units held by the validator, and every disagreement between a decision and the
> raw file headers after download, were resolved by a person ({n_queue} of {n_units} units). A unit entered
> analysis only after a person approved a manifest digest covering its decision files. Agreement between
> independent triages was {kappa_file} (Cohen's kappa, per file) on a stratified sample of {n_sample}
> accessions, and accuracy on an evaluation set of {n_eval} accessions with known assignment was {acc}
> (Figure {x}). All steps after the decision files used pinned code and reproduce the MS-DIAL input files
> exactly from the released decision files without the language model (Supplementary Table {t}).

Fill the braces from `replay-report.json`, `rederivation/agreement.json` and `tests/triage_eval.py`.

### 5.2 Supplementary table: `supplementary/llm-decisions.tsv`

Written by `scripts/export-llm-decisions.py <campaign dir>`, one row per decision (revisions as their own rows):

`decision_id, kind, accession, unit_id, supersedes, model_id, skill, skill_version, skill_sha256,
prompt_template_sha256, inputs_sha256 (sha256 of the sorted input hashes), decided_at, n_rows_kept,
n_rows_excluded, n_members_added, n_declared, n_inferred, n_low_confidence, status, validator_verdict,
validator_commit, gate_verdicts, n_header_disagreements, person_review (none, answered, revised),
reviewer, approved_manifest_digest, final_state (accepted, revised, excluded, withdrawn),
rederivation_sampled, rederivation_agreement, replay_selection_equal, replay_inputs_equal`

### 5.3 What is released (Zenodo, with the paper)

Released: the skill, prompt templates, schemas, validators and replay scripts at their commits; the eval set
with its versions; every decision file, envelope, `inputs\` directory and review log; every campaign and run
manifest; `replay-report.json`; the re-derivation sample and agreement; redacted transcripts.

Not released: private library content (VS20/VS21 MSP, laboratory LBM2), named only by file name and sha256,
or any path to it; raw data, referenced by accession, archive name, size and CRC32. `scripts/check-release-redaction.py <bundle>` refuses a bundle that contains a
private library name with a path, a drive letter path outside the bundle, or a user profile path.

## 6. Failure handling

- **LLM against header evidence.** After download, header preflight is reconciled against each kept file
  (ion mode, MS levels, DIA window signature, run end time). A disagreement writes a `header_reconciliation`
  entry with `result: disagree`, holds the unit and opens a `folder_header_disagreement` question. Neither
  side wins silently: the person accepts the header, accepts the triage or excludes the file, and the cause is
  logged as a `revision` when the selection changes.
- **Validator error.** The triage is redone; the errored payload stays in the bundle with its verdict.
- **Gate refusal** (rules P, C, D). The unit ends `attribution_refused`, is not retried, and its done outputs,
  if any, are withdrawn from redistribution.
- **Counts reported.** `scripts/export-llm-decisions.py --summary` writes `supplementary/llm-summary.json`:
  units triaged; units by status; files kept, excluded and abstained; validator errors and holds by check;
  person queue size by question code; header disagreements by field and by resolution; revisions; units
  withdrawn. The Methods and Figure {x} cite these numbers only.

## 7. Build order

Schemas and skill 1.1.0; `validate-triage.py` (v2, envelope hashes, `--emit-selection`, log append); the four
new scripts; plan and runner fields; Interactive `curated_selection` and canonical hashes (next free patch
version); `replay-campaign.py` with a synthetic fixture test per level.
