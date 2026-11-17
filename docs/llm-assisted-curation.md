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
| Deterministic code | schema and contract checks (`scripts/validate-triage.py`); coverage and cross-unit attribution gates (Interactive, rules P, C, S and D, section 6); manifest and digest (`scripts/campaign/plan.py`); download with checksum; header preflight and its reconciliation against the triage (polarity and acquisition mode family); analysis CSV; MS-DIAL Console; mzTab-M validation; QA | verdicts in the review log and the decision ledger (section 2.4), manifests, run manifests |
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
<campaign dir>\handoffs\<unit_key>\                     the runner's run-time copies (section 2.5)
    handoff.json                the Catalog handoff copy Interactive reads, its sidecars beside it
    handoff.original.json       the Catalog handoff's bytes exactly as taken at the handoff step
    handoff.triaged.json        the triaged handoff's bytes, kept when the one taken differs only in what the Class decides
    <unit_id>.json              a byte copy of the triage payload
    <unit_id>.record.json       a byte copy of its decision record
```

**Order: the triage comes before the Class.** The triage runs first, read only, before any Class proposal of
the unit is saved. It binds only the evidence it read: the Catalog's rows, files and listing, the repository's
declarations and the archive listing. It never reads or cites the handoff's `class_proposal` (its assignments,
labels, contrast, status or ratification), and never binds it as a decision record input. `validate-triage.py`
refuses a triage whose evidence citations (field, value or detail), or whose decision record inputs, name the Class
proposal, its assignments or labels, `ratified_by` or the ratification (error `T-CLASS`); the triage must then be
redone. The Class is settled afterwards. The plan records the Class decision's proposal id as the approved Class
digest, and the runner saves and ratifies that proposal under the campaign approval (boundary 3) just before it
takes the handoff. So the handoff the triage read normally carries no Class, or one that is not yet ratified. The
two bind separately: the triage binds its evidence through `catalog_handoff_sha256` and the handoff comparison,
and the Class binds through its own ratification and the class digest (section 2.5).

`<unit_key>` is the campaign's unit key: the Catalog unit id, or `<unit_id>-<child_key>` for a child. The
unit's workspace folder is its key, or `<unit_key>@<campaign_id>` for a reopened unit (`workspace_name` in the
manifest), so a reopened run never writes into the earlier run's folder. The copies are checked against the
manifest's sha256 at the handoff step. The record copy is not what Interactive reads: its `inputs[]` resolve
only beside the original record, so the runner passes the original as `decision_record_path` (section 2.5).

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

**Inputs the build reads.** Interactive (`curated_selection.read_decision_record`) and the runner
(`curated.record_inputs`) read two roles of `inputs[]`, resolving each `path` relative to the record file's own
directory. Interactive checks every `archive_listing` and `catalog_handoff` input against its `sha256`; the
plan checks the `archive_listing` inputs. The listings are parsed as section 3.1 says and merged in record
order, the first row of a member winning, into the listing the selection takes crc32 and size from. The
`catalog_handoff` input is the triaged handoff, which Interactive compares the run-time handoff copy with
(section 2.5). The record's `payload.sha256` must be the triage file's sha256. A record
that breaks any of these refuses the unit: Interactive's code is `curated_selection_record_mismatch`, and the
plan leaves the unit out as `triage_record_mismatch` before a person sees the digest.

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
transcript; the sha256 of each `context_files` item. In a release bundle, an `inputs[]` file released as a
projection (section 5.3) is checked through `release-index.json` instead: its bytes must hash to the index's
`release_sha256`, and the index's `original_sha256` must be the item's `sha256`. It also refuses a tool call not on the skill's allowed
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
files before and at the approval, and the answers people give about them. Each entry carries `seq`
(counting from 1), `at`, `kind`, `decision_id`, `prev_sha256` and exactly the fields of its kind:

| kind | fields | written by |
|---|---|---|
| `validator_verdict` | `tool` (`validate-triage.py`), `code_commit`, `verdict` (pass, hold, error), `findings_sha256`, `findings` (check, level, message) | `validate-triage.py --emit-selection` |
| `person_answer` | `by` (name or the initials the person agreed to publish), `question_code`, `answer` (verbatim), `channel` (`conversation`) | the runner, from a `skip`, `retry` or `release-held` request on a curated unit that carries `--question-code` and a named `--by`; the request's `--reason` is the answer, verbatim, as that person gave it in the conversation (the rule of `scripts/record-reading.py`) |
| `revision` | `supersedes`, `new_decision_id`, `cause` (`{kind, seq}` of a `person_answer`, a ledger `header_reconciliation` or a `validator_verdict`) | the agent |
| `digest_approval` | `manifest_digest`, `approval_id`, `by` (copied from the ledger's `manifest_approved`) | `campaign-runner.py approve`, one entry per decision the approved manifest covers (the children of one triage share it) |

The runner's writer (`curated.append_review_log`) refuses any other kind and any field outside its kind's
row, and refuses a `person_answer` without `by` and `question_code`. The CLI refuses `--question-code`
without `--by`. A request that carries no question code, or names no person, is the operator's: it is an
`operator_request` campaign event, kept in the campaign's `ledger.sqlite`, never a `person_answer`, and not
exported to the decision ledger. Gate and header verdicts never enter the review log.

**`decision-ledger.jsonl`** (one per campaign, in the campaign directory) is exported by
`campaign-runner.py decisions --campaign <id>` from the campaign's events, appending only: it refuses, and
writes nothing, when a line already on disk is not the one the events give. Each line is `{seq, event_seq,
at, kind, unit_key, standing, detail, prev_sha256}`, where `standing` is one of `source_fact`,
`model_inference`, `deterministic_check`, `accepted_decision` and `person_answer`. Every path in `detail` is
relative to the workspace root, or `<external>/<file name>` outside it; a ledger line never holds an absolute
path, because the file is released as it is and its chain forbids a later rewrite. The gate and header
verdicts are recorded here, not in the review log:

| ledger kind | standing | written by | records |
|---|---|---|---|
| `triage_recorded` | model_inference | the approval (`plan.py`) | `triage_relative_path`, `triage_sha256`, `triage_record_sha256`, `catalog_handoff_sha256`, `catalog_rows_sha256`, `archive_listings`, `selection_listings`, rows kept, members added, rows dropped by reason, kept by basis and confidence, flags, `selection_sha256`, `selection_canonical_form`, `analysis_inputs_sha256`, `workspace_name` |
| `triage_validated` | deterministic_check | the approval | `validator_sha256`, `verdict`, `report_sha256`, `review_log_head_sha256` |
| `manifest_approved` | accepted_decision | the approval | `manifest_digest`, `approval_id`, `approved_by`, `covers`, `selections` (unit key to `selection_sha256`), `validator_sha256` |
| `unit_reopened` | accepted_decision | the approval | `reason_code`, `evidence_sha256`, the evidence file name, the new workspaces |
| `triage_checked` | deterministic_check | the runner, at the handoff step | `verdict` (`pass`, `refused`, `held`), `handoff_identity` (`handoff_identical`, `values_identical` or `class_only`) with the `class` record (both sides' Class, as Interactive's `original_handoff.class`), `class_binding` (whether the copy's Class is the approved proposal, accepted and ratified under this campaign), the reason (`triage_digest_changed`, `triage_record_digest_changed`, `class_digest_changed`, `class_not_ratified`, `handoff_copy_changed`, `triage_handoff_changed` with `rows_identical` and `differing_keys`, `sidecar` or `triaged_bytes_found`), the copies' paths and the handoff hashes |
| `curated_selection_applied` | accepted_decision | the runner, after the download | `plan_selection_sha256`, `interactive_selection_sha256`, `selection_sha256_equal`, `selection_canonical_form`, rows kept and dropped, what Interactive recorded (`manifest_digest`, `standing`, `child_key`, `analysis_unit_key`, `catalog_handoff_verification`, `triage_record_sha256`), and `handoff_class`, Interactive's `original_handoff.class` |
| `header_reconciliation` | deterministic_check | the runner, after the preflight | Interactive's reconciliation block (section 6): `schema`, `verdict` (or `not_recorded`), `reason`, `inspected`, `agree`, `disagree`, `kept_rows_not_inspected`, `disagreements_by_field`, and every per file item copied whole |
| `cross_unit_verdict` | deterministic_check | the runner, once per download group, and once for a curated unit alone in its group | `group`, `units`, `manifest_paths`, `verdict` (`passed`, `refused`, or `not_applicable` with reason `single_member`), `refused` (per unit `reason_code`, `reasons`, `rule` P, C, S or D, `refused_against`), `refused_after_pass` (units an earlier check passed), `unit_verdicts`, and `interactive` (`verdict`, `rule`, the rule id such as `cross_unit_attribution_2026_11_16b`, `schema`, `overlaps`, `version`) |
| `terminal_refusal` | deterministic_check | the runner | a refusal that ends the unit: attribution coverage; `attribution_refused` (a curated selection Interactive refuses, the cross-unit check), with `invalidated_after_done: true`, `outputs: kept_flagged_invalid` and `question_code: cross_unit_refused_after_done` for a unit that had run (section 6); and policy drift (`reason: policy_drift` with the code, for a unit stopped on a changed manifest, triage, record, handoff or Class decision, `class_digest_changed`) |
| `person_answer` | person_answer | the runner | `by`, `question_code`, `answer`, `channel`, and what the request did (`action`, `request_id`, `requested_at`, `state_before`, `state_after`, `result`) |

`scripts/verify-decision-log.py <campaign dir>` walks every review log and the ledger. It refuses a broken
link; a `decision_id` without its payload; a revision whose `cause` names no entry of the stated kind; a
revision that removes a blocking flag without a `person_answer` of the matching `question_code` (section
2.2); a ledger `triage_recorded` whose triage sha256 matches no payload; and a `triage_validated` whose
`review_log_head_sha256` is not the hash of a line of that accession's review log.

### 2.5 Manifest and run manifest

**The plan.** `campaign-runner.py plan --triage-dir <dir>` (`scripts/campaign/plan.py`,
`curated.unit_entries`) writes one manifest entry per unit, or one per child keyed `<unit_id>-<child_key>`.
Each entry has a `curated_selection` block:
- the triage: `schema`, `triage_path`, `triage_relative_path`, `triage_sha256` (also as `sha256`),
  `triage_record_sha256`, `review_log_head_sha256`, `catalog_handoff_sha256`, `catalog_rows_sha256`;
- the validator: `validator` (`script_sha256`, `verdict`, `report_sha256`);
- the selection: `selection_listings` (path, sha256 and entry count of each listing the record binds),
  `selection_canonical_form`, `selection_count`, `selection_sha256` (section 3.1, over the record's listing),
  `analysis_inputs_sha256`;
- the rows: `child_key`, `rows_kept`, `members_added`, `rows_dropped`, `rows_dropped_by_reason`.

The digest a person approves therefore covers the frozen decisions and every review entry before it. The
plan leaves a unit out, with its reason, when its triage is `triage_missing`, `triage_unreadable`,
`triage_not_pass`, `triage_member_unresolved`, `triage_record_mismatch` or
`class_contrast_lost_after_triage`.

**The handoff step.** The runner copies the Catalog handoff and the triage into `handoffs\<unit_key>\`
(section 2.1). The triage copy must hash to `triage_sha256` and the record copy to `triage_record_sha256`.
If either does not, the unit ends `stopped_policy_drift` (`triage_digest_changed`,
`triage_record_digest_changed`) and nothing is downloaded. The Catalog handoff taken now must be the triaged
one (`catalog_handoff_sha256`), byte for byte or in every value Interactive compares (below).

The Catalog embeds the unit's Class in the handoff: the `class_proposal` object (the proposal, its assignments,
contrast, status, ratification and `ratified_by`), and the readiness it derives from it. The triage was made
before that Class was saved or ratified (section 2.1), and the runner saves and ratifies the approved proposal
under its own approval (boundary 3) just before it takes the handoff. A handoff taken under the campaign
therefore normally does not hash to the triaged one. The triage binds the evidence it read; the Class binds
through its own ratification. The handoff step checks the two separately.

*The Class, by ratification.* For every curated unit, a byte identical handoff included, the runner checks the
copy's `class_proposal` (`curated.class_binding`). It must be the approved proposal (its id is the approved Class
digest), saved for this unit, `accepted`, and ratified under this campaign: `ratified_by` and the ratification's
`approval_id`, `manifest_digest`, `authorization_sha256` and `campaign_id` must be this campaign's, and the
ratification must name the proposal. A ratified abstention passes the same way. A copy that carries another
proposal ends `stopped_policy_drift` (`class_digest_changed`), as the class digest drift stop at the Class step
does. A copy whose Class is missing, saved for another unit, not accepted or not ratified under this campaign is
held (`contract_held`, `class_not_ratified`); its recheck settles the Class again from the start (boundary 3,
with its digest check) and takes the handoff again. `triage_checked` records the result as `class_binding`. A
copy with no `class_proposal` key at all carries no Class either: the Catalog always writes the key (`null` where no
proposal is named), so a copy without it is held the same way (`class_not_ratified`, with `class_proposal_key: false`)
and never bound. The Class is read from the bytes the copy step hashed (`copy_sha256`); a copy that no longer hashes so
when it is read is held (`handoff_copy_changed`) and its recheck takes the handoff again.

*The evidence, by the triage.* When the bytes differ, the runner reads the triaged bytes, first from the decision
record's `catalog_handoff` input, then from `inputs\<unit_id>\handoff.json`, and accepts only those whose sha256
is `catalog_handoff_sha256`. It compares them with the handoff taken now exactly as Interactive's
`verify_handoff_copy` does (`curated.handoff_difference`), leaving out the rewritten sidecar paths and what the
Class decides, `HANDOFF_CLASS_KEYS`:
- `keys`: the whole `class_proposal`;
- `blocking_reason_codes`: `class_proposal:missing`, `class_proposal:not_accepted` and
  `class_proposal:other_unit`, the codes the Catalog's handoff builder adds from the Class alone.
  `blocking_reasons` is compared with only these codes removed, so any other code appearing or disappearing
  (`technical_metadata:*`, `analysis_input:*`, an unknown `class_proposal:*`) is still a difference;
- `readiness_keys`: `next_action` and `ready_for_download_planning`, which the builder derives from the blocking
  reasons. They are left out only when the remaining (non Class) `blocking_reasons` are equal on both sides;
  otherwise they are compared like any key.

Everything else is compared: the files, the sample rows, the technical settings, the sidecars' contents, the unit
and accession, and every other top level key. The runner's `HANDOFF_CLASS_KEYS` is Interactive's, copied, and a
test asserts the two are equal and that both sides decide the same cases alike. When only the Class differs, the
runner keeps the triaged bytes as `handoff.triaged.json` and passes them as `original_handoff_path`.
`triage_checked` records `handoff_identity: class_only`, or `values_identical` when even the `class_proposal` is
equal, with the `class` record.

A triage made before any Class was saved, before the Class was ratified, or under another campaign's ratification
therefore passes on its evidence, and the Class it did not read is bound by the ratification check. A handoff that
differs in anything else is held for a person (`contract_held`, `triage_handoff_changed`, with `differing_keys` or
the differing `sidecar`). So is a handoff whose triaged bytes are kept in neither place (`triaged_bytes_found:
false`). It is never passed because its rows look the same.

**The download.** The runner calls `msdial_download_repository_raw` with:

| argument | value |
|---|---|
| `analysis_unit_handoff_path` | `handoffs\<unit_key>\handoff.json` |
| `curated_selection_path` | `handoffs\<unit_key>\<unit_id>.json` |
| `decision_record_path` | the original `<campaign dir>\triage\<accession>\<unit_id>.record.json`, beside the `inputs\` it binds |
| `original_handoff_sha256` | the triage's `catalog_handoff_sha256` |
| `original_handoff_path` | the triaged bytes: `handoffs\<unit_key>\handoff.original.json` when the handoff taken is the triaged one byte for byte, else `handoffs\<unit_key>\handoff.triaged.json` |
| `curated_selection_validator` | the manifest's `validator` block, recorded as given |
| `curated_selection_child_key` | the child's key, for a child |
| `workspace_unit_key` | the workspace folder, when it is not the Catalog unit id (a child named otherwise, a reopened unit) |
| `campaign_authorization_path` | the campaign's approval |

Before the call the runner hashes `handoffs\<unit_key>\handoff.json` again, with the sidecars it names
(`files.json`, `samples.json`, `inputs.json`), and compares each with the sha256 the handoff step recorded
(`copy_sha256`). The handoff step checked the Class in those bytes, and Interactive compares the copy with the triaged
handoff leaving out what the Class decides, so a copy changed in between would reach Interactive with a Class nobody
checked. It is never passed: the unit is held (`contract_held`, `handoff_copy_changed`, naming the file) with nothing
downloaded, and its recheck takes the Catalog handoff again (from `class_settled`) and checks it again.

`msdial_repository_reanalysis_plan` takes the same arguments. `msdial_repository_batch_plan` takes them per
unit, in `curated_selection_paths` and `workspace_unit_keys`. Without `decision_record_path`, Interactive
reads `<triage file stem>.record.json` beside the triage. For a stem that is neither the unit id nor a
revision `<unit_id>.r<N>`, it then tries `<unit_id>.record.json`. A named record that does not exist refuses
the unit.

`workspace_unit_key` sets only the folder `<root>\<repository>\<accession>\<key>`. Its rules:
- it matches `[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}`;
- it holds no `..` and does not end in `.`;
- it is not a name the workspace keeps for itself;
- it equals the Catalog unit id, or starts with it followed by one of `- _ . @ +`.

The plan applies the same rules (`curated.workspace_name_problem`) to every child and reopened workspace name.
It stops with an error naming the unit before a person sees the digest, so a campaign id such as `pilot.` or
`a..b` is refused at plan time, never at the download.

Interactive records the key as `project.repository_metadata.workspace_unit_key`. The analysis unit id
(`<unit_id>` or `<unit_id>-<child_key>`), campaign coverage and store claims are unchanged. A refusal recorded
in an earlier workspace is not carried into a reopened one.

**What Interactive checks before any byte moves.**
- **The record** (section 2.3).
- **The handoff identity.** When the copy's bytes hash to `original_handoff_sha256`, verification is
  `handoff_file`. Otherwise Interactive reads the triaged bytes from `original_handoff_path`, else from the
  record's `catalog_handoff` input, whose recorded sha256 must be the asserted one. It compares them with the
  copy (`verify_handoff_copy`). Every key except `file_manifest_path`, `sample_table_path` and
  `analysis_input_manifest_path` must be equal, and so must the sidecar contents, except what the Class decides
  (`curated_selection.HANDOFF_CLASS_KEYS`, the same set as the runner's above): the whole `class_proposal`; the
  `blocking_reasons` codes `class_proposal:missing`, `class_proposal:not_accepted` and
  `class_proposal:other_unit`, with `blocking_reasons` compared without them; and `next_action` and
  `ready_for_download_planning`, left out only when the remaining `blocking_reasons` are equal. Any other
  difference refuses the unit (`curated_selection_handoff_mismatch`). Verification is then
  `original_read_and_copy_compared`. With nothing to compare against, the unit is refused.

  The run uses the copy's Class. Interactive does not check that it was ratified under the campaign; the copy's
  own `blocking_reasons` hold it as any handoff's do, so a missing or not accepted Class is a download blocker. A
  Class a person confirmed but nobody ratified carries no blocker of its own, so the runner's ratification check
  (`class_not_ratified`) is what holds it.
- **The approval.** Under a campaign approval, the manifest entry is matched by `unit_key` or `unit_id`. It
  must carry the same `triage_sha256`, and the same `triage_record_sha256` and `selection_sha256` where it
  names them. A `selection_sha256` that differs from Interactive's own value refuses the unit with
  `curated_selection_selection_mismatch`.

Every refusal is `retryable: false`. The codes are `curated_selection_invalid`, `_not_ready`,
`_handoff_mismatch`, `_not_approved`, `_record_mismatch`, `_selection_mismatch`, `_child_unknown`,
`_mixed_polarity`, `_member_unresolved`, `_empty` and `_class_contrast_lost` (each after the prefix
`curated_selection`), and `triage_header_disagreement` (section 6).

**What Interactive records.** In `provenance\run-manifest.json`, under
`project.repository_metadata.curated_selection` (`msdial-curated-selection.v1`), Interactive records:
- the triage: `triage_schema`, `path`, `sha256`, `triage_sha256`, `triage_record_sha256` and `triage_record`
  (`name`, `path`, `sha256`, `decision_id`, `located` (`explicit` or `beside_triage`), `archive_listings`,
  `catalog_handoff_input`);
- the handoff: `catalog_handoff_sha256`, `catalog_handoff_verification`, `original_handoff` (only when
  compared, with `source` and `class` `{excluded_keys, excluded_blocking_reason_codes, readiness_keys,
  readiness_left_out, original, copy, differs}`, each side holding its `class_proposal`, its Class blocking codes,
  `next_action` and `ready_for_download_planning`), `original_handoff_sha256`
  and `copy_handoff_sha256`;
- the unit: `analysis_unit_key`, `catalog_analysis_unit_id`, `child_key`, `phase`, `decided_by`,
  `decided_at`, `purpose`, `unit_technical`, `applied_technical` and `validator`;
- under an approval: `approval_id`, `manifest_digest` and `manifest_selection_sha256`;
- the selection: `selection_sha256`, `selection_canonical_form`, `selection_listing_entries` and
  `selection_sha256_matches_manifest` (null when the manifest names no value);
- the rows: `rows_kept`, `rows_dropped` and their counts, `added_members_not_admitted`, `flags` and
  `rationale`;
- `standing` (`accepted_decision` under an approval, else `model_inference`) and `applied_by`.

Each admitted input's lineage row carries `selected_by: curated_selection`, `triage_row_index` and
`triage_member_path`. Interactive writes `analysis_csv.canonical_sha256` together with the analysis CSV. It
writes `replay_projection` and `replay_projection_sha256` only at finalisation (section 3.2). Readers take the
block from `project.repository_metadata`, and fall back to an older top-level `curated_selection`.

**The preflight.** The runner holds a unit whose manifest records no curated selection with the approved
`triage_sha256` (`contract_held`, `curated_selection_not_recorded`). It also compares the `selection_sha256`
Interactive recorded with the one in the approved manifest, and holds the unit when they differ
(`contract_held`, `curated_selection_selection_mismatch`). The ledger's `curated_selection_applied` records
both values.

## 3. Replay without any LLM

A replay starts from the release bundle (section 5.3) and pinned code. The pinned code is the reanalysis
repository commit, the Interactive and Catalog versions and commits, the Console commit and binary sha256,
and the raw metadata extractor pin. All of these are already in the manifest.

```text
<python> scripts/replay-campaign.py --bundle <release bundle or campaign dir> --workspace-root <absolute path>
    --level selection | inputs | outputs   [--unit <unit key>] [--libraries <dir>] [--out <report>] [--json]
```

`--bundle` is the release bundle (it holds `release-index.json`). It can also be a campaign directory as the
runner keeps it, for a replay on the producing machine. The exit status is 0 for equal, 1 for different and
2 for not evaluable; a check that could not run never gives 0. The script writes `replay-report.json`
(`msdial-campaign-replay-report.v1`):
- the report fields: `level`, `result`, `bundle` (`release` or `campaign_directory`), `manifest_digest` and
  `campaign_id`;
- one row per unit, with a first row `(campaign)` for the digest;
- per row, its `checks` (`check`, `result`, `expected`, `replayed`, and `why` where a check is not equal) and
  the `projection_schema` it compared under.

The selection it rebuilds is written to `replay-inputs\<unit key>.json`. The replay module imports no agent or
model client; a test asserts this. Each level includes the one before it.

| Level | Needs | Recomputes | Equal when |
|---|---|---|---|
| `selection` | the bundle and `validate-triage.py` at its commit; no network, no raw data, no Console | per unit: `triage_sha256`, `triage_record_sha256`, the review log chain and the head the plan recorded, the validator's verdict and sha256, `selection_sha256` over the listing the record binds (section 3.1), `analysis_inputs_sha256`; for the campaign: the manifest digest as described below | every recomputed value equals the recorded one |
| `inputs` | the raw archive members, downloaded and extracted by the third party under `<workspace root>\<repository>\<accession>\`; for the run manifest hashes, the third party's own Interactive run of the unit through finalisation, which needs a Console run (below) | each selected member's size and CRC32 against the listing; the hashes the recorded projection schema names, read from the third party's `provenance\run-manifest.json`; the analysis CSV's canonical sha256 | the members are equal and every hash the schema names is equal (section 3.2) |
| `outputs` | the pinned Console; the same libraries (`--libraries`, matched by sha256); the bundle's `units/<unit key>/output-checksums.tsv` | the canonical sha256 of each finalised MS-DIAL text output under the unit workspace's `output\` | every output the checksum file lists is equal (section 3.3) |

**The manifest digest.** From a campaign directory, the replay recomputes the digest from the bytes of
`campaign-manifest.json` and compares it with `campaign-authorization.json`, or with the plan's
`campaign-manifest.summary.json`. A release bundle does not hold the original manifest, because the original
carries absolute paths (`workspace_root`, `triage.directory`, `triage_path`). From a release bundle, the replay
checks two things. The index's `original_sha256` must be its `approved_manifest_digest`, the value the
ledger's `manifest_approved` also records. And the released projection must hash to the index's
`release_sha256`. The original's sha256 itself can be recomputed only on the producing machine.

**Triage inputs released as projections.** A Catalog handoff under `inputs\` holds absolute sidecar paths, so
the release writes it as a projection (section 5.3). From a release bundle, the replay first checks each
projected handoff against the index's `release_sha256`. It then gives `validate-triage.py` the same rows as a
`catalog.json` whose handoff sha256 is the index's `original_sha256` (`curated.released_catalog`). The validator
compares that value with the triage's `catalog_handoff_sha256` (T-HANDOFF), and the triage is released byte for
byte under its `triage_sha256`. A projection whose bytes are
not the ones the index names makes the row's `released_triage_inputs` check `different`, and its original
sha256 is never taken on trust.

**What needs a Console run.** No level before `outputs` runs MS-DIAL on purpose, but two values at the
`inputs` level exist only after a Console run:
- **`members` and `analysis_csv.canonical_sha256`.** These need the raw archives and Interactive's download,
  preflight and analysis CSV steps, and no Console.
- **`replay_projection_sha256`.** Interactive writes it only at finalisation (`finalize_download_lease`).
  That step comes after the zero-threshold diagnostic, the production run of the pinned Console with its
  libraries, and the mzTab-M validation. The v1 projection also holds the accepted peak height from that
  diagnostic and the `curated_selection.manifest_digest` of the approval the unit ran under. Interactive
  takes that digest only from an approval whose named manifest still hashes to it.

So outside the producing machine, `replay_projection_sha256` is `not_evaluable` when the third party's
manifest has no projection. A finalised run there gives `different`, because it cannot run under the
original approval. A third party's `inputs` row is therefore not evaluable as a whole. Its `members` and
`analysis_csv.canonical_sha256` checks are still reported one by one, and the Methods count those (section
5.1). The levels report `not_evaluable` with these reasons:
- **`selection`:** no triage file, no decision record, or a listing the record binds is missing or changed. (A
  projected triage input that is not the bytes the index names is `different`, not `not_evaluable`.)
- **`inputs`:** members not found; no recorded or replayed run manifest; a projection schema the replay does
  not know; or no `replay_projection_sha256`, because Interactive writes it at finalisation, after a Console
  run.
- **`outputs`:** `private_library`; no `output-checksums.tsv` in the bundle; or no replayed `output\`
  directory (needs a Console run).

### 3.1 The selection (`msdial-selection-canonical.v1`)

The selection is the triage's decision: every file the payload keeps, its `rows[]` and its
`added_members[]`, whether or not a given Interactive version admits added members (0.5.69 records them
under `added_members_not_admitted`; what was analysed is fixed by `analysis_csv.canonical_sha256`). The
definition is normative. Every implementation gives the same value for the same unit:
- **Interactive** (0.5.69) implements it in `msdial_app.curated_selection`, which exposes
  `selection_canonical` (alias `selection_entries`), `selection_sha256`, `parse_archive_listing` and
  `SELECTION_CANONICAL_FORM` (`"msdial-selection-canonical.v1"`).
- **The runner** and `replay-campaign.py` carry an unchanged copy in `scripts/campaign/curated.py`, so a replay
  runs without Interactive. Their tests assert that the copy gives Interactive's value on every eval fixture,
  with and without a listing and per child, and gives the two hashes of the worked example below.

The reference and the implementations also accept a child's `child_key` string in place of the child. An
empty string is the whole unit, an unknown key raises `ValueError`, and a string passed as the listing raises
`TypeError`.

- **Fields.** `unit_id` is the Catalog unit id, bare, also for a child; `child_key` is the child's key or
  `null`; `member_path` is the triage's own, with `\` turned into `/` and leading and trailing `/` removed,
  case kept; `archive_name` is the triage item's `archive`, else the listing's; `sample_row_index` is the
  row's `row_index`, `null` for an added member.
- **Sources.** `crc32` and `size` come from the archive listing the decision record binds, never from the
  triage and never from a download. That listing is every `inputs[]` item with `role: archive_listing`,
  parsed by `parse_archive_listing` (a TSV with columns `archive`, `path`, `size` and `crc32`, others ignored;
  keyed on the case folded member path; the first row of a path wins; a size that is not a whole number
  refuses) and merged in record order. Without such a listing, or without a match, both are `null`. A reader
  holding a listing whose sha256 differs from the record's refuses. The plan and Interactive therefore build
  the same listing from the same record, and Interactive refuses a unit whose approved `selection_sha256`
  differs from its own (`curated_selection_selection_mismatch`).
- **Children.** A child keeps the items whose member path is in its `member_paths`.
- **Order.** Sorted by `unit_id`, `child_key` (null as empty), `member_path`, `sample_row_index` (null as
  -1), then by the entry's canonical JSON.
- **Hash.** `selection_sha256` is the sha256 of the canonical JSON (sorted keys, no spaces, UTF-8, no ASCII
  escaping) of the sorted list.

```python
# msdial-selection-canonical.v1 reference implementation
import csv
import hashlib
import io
import json

SELECTION_CANONICAL_FORM = "msdial-selection-canonical.v1"


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _member(path):
    return str(path or "").replace("\\", "/").strip().strip("/")


def parse_archive_listing(data, listing=None):
    """An archive listing TSV (bytes) as the selection reads it, merged into `listing` when given."""
    found = listing if listing is not None else {}
    for record in csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter="\t"):
        member = _member(record.get("path"))
        if not member:
            continue
        size = str(record.get("size") or "").strip()
        if size and not size.isdigit():
            raise ValueError(f"The archive listing gives size {size!r} for {member}.")
        found.setdefault(member.casefold(), {
            "archive": str(record.get("archive") or "").strip() or None,
            "crc32": str(record.get("crc32") or "").strip() or None,
            "size": int(size) if size else None,
        })
    return found


def _child(triage, child_key):
    for child in triage.get("children") or []:
        if isinstance(child, dict) and str(child.get("child_key") or "") == child_key:
            return child
    return None


def selection_canonical(triage, listing=None, child=None):
    """listing: {case folded member path: {"archive", "crc32", "size"}} from the record's archive_listing inputs,
    {} or None when it binds none. child: one item of triage["children"], its child_key, or None (or "") for the
    whole unit."""
    if isinstance(listing, str):
        raise TypeError("listing is a mapping, not a child key")
    listing = listing or {}
    if isinstance(child, str):
        found = _child(triage, child) if child else None
        if child and found is None:
            raise ValueError(f"the triage has no child {child!r}")
        child = found
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


selection_entries = selection_canonical


def selection_sha256(triage, listing=None, child=None):
    return hashlib.sha256(canonical_json(selection_canonical(triage, listing, child))).hexdigest()
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
  `../<path>`, outside it as `<external>/<file name>`), forward slashes, blank lines dropped, LF line endings,
  UTF-8 without BOM. Interactive writes it with the analysis CSV; `curated.canonical_analysis_csv` is the
  runner's copy of the rule.
- **`parameter_file.canonical_sha256`** (`msdial-parameter-file-canonical.v1`): the Console parameter file
  with paths rewritten by the same rule, every library path replaced by `<library:<file name>
  sha256=<sha256>>`, trailing whitespace removed, LF line endings, UTF-8 without BOM. Interactive records it
  in the run manifest from the next free patch version after 0.5.69.
- **`replay_projection_sha256`**: the sha256 of the canonical JSON of `replay_projection`, a whitelist of the
  run manifest. Timestamps, job ids, host names and absolute paths are excluded by construction. The run
  manifest names the schema as `replay_projection.schema`. Interactive writes both at finalisation
  (`finalize_download_lease`), after the Console run and the mzTab-M validation, whatever the validation's
  result. Nothing writes them earlier, so this hash needs a Console run (section 3).
  - `msdial-replay-projection.v1` (Interactive 0.5.69, `curated_selection.replay_projection`):
    - the unit: `schema`, `repository`, `accession`, `analysis_unit_id` and `catalog_handoff_sha256`;
    - `curated_selection`: `triage_sha256`, `triage_record_sha256`, `selection_sha256`, `manifest_digest` and
      `child_key`;
    - the settings: `ion_mode`, `acquisition_mode` and `separation`;
    - `inputs`, sorted by member: `member`, `converted_to` for an mzML converted from an mzXML member,
      `identity`, `identity_basis`, `sample_id`, `acquisition_type` and `header_polarity`;
    - `analysis_csv_canonical_sha256`;
    - `peak_height` from the zero-threshold diagnostic: `minimum_peak_height`, `diagnostic_peak_count`,
      `threshold_step` and `method`;
    - `msdial_interactive_version`.

    It does not cover libraries, the Console identity or the parameter file. Because it holds the
    approval's `manifest_digest`, it is equal only for a run under an approval of the same manifest.
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

The bundle records the expected values in `units/<unit key>/output-checksums.tsv`, with the columns `path`
(relative to the unit workspace's `output\`) and `canonical_sha256`, or `sha256` where no canonical value
was recorded. The canonical value is the sha256 after every tab separated cell that is an absolute path is
rewritten by the rule of section 3.2, with LF line endings and UTF-8 without BOM. `campaign-runner.py
release` does not write this file yet. Until finalisation records canonical output hashes and the release
writes them, every unit is `not_evaluable` at `outputs`, and the Methods make no outputs claim.

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
and `tests/triage_eval.py`. Two of them need a definition:
- `{n_selection_equal}` counts the units whose `selection` level row is `equal`.
- `{n_inputs_equal}` counts the units whose `members` and `analysis_csv.canonical_sha256` checks are both
  `equal` at the `inputs` level. These two checks need no Console run. The row as a whole also needs
  `replay_projection_sha256`, which a third party cannot make equal (section 3).

Each projection schema a count rests on is stated beside it. No outputs claim is made while every unit is
`not_evaluable` at `outputs` (section 3.3).

### 5.2 Supplementary table: `supplementary/llm-decisions.tsv`

Written by `scripts/export-llm-decisions.py <campaign dir>`, one row per decision (revisions as their own rows):

`decision_id, kind, accession, unit_id, supersedes, revision_cause, model_id, agent_context, skill,
skill_version, skill_sha256, prompt_template_sha256, inputs_sha256 (sha256 of the sorted input hashes),
decided_at, n_rows_kept, n_rows_excluded, n_members_added, n_declared, n_inferred, n_low_confidence, status,
validator_verdict, validator_commit, gate_verdicts, n_header_disagreements, person_review (none, answered,
revised), reviewer, approved_manifest_digest, final_state (accepted, revised, excluded, invalidated, withdrawn),
rederivation_sampled, rederivation_agreement, replay_selection_equal, replay_inputs_equal,
replay_projection_equal, replay_projection_schema`

`replay_inputs_equal` is the `{n_inputs_equal}` criterion of section 5.1, the `members` and
`analysis_csv.canonical_sha256` checks. `replay_projection_equal` is the `replay_projection_sha256` check:
`equal`, `different` or `not_evaluable`, with the reason.

### 5.3 What is released (Zenodo, with the paper)

**The campaign bundle.** First `campaign-runner.py decisions --campaign <id>` exports the ledger. Then
`campaign-runner.py release --campaign <id> --out <empty folder>` (`curated.write_release`) writes the files
`replay-campaign.py` reads:

```text
<bundle>\
    release-index.json                          msdial-release-index.v1
    campaign-manifest.release.json              the campaign manifest's release projection
    units\<unit key>\run-manifest.release.json  each unit's run manifest's release projection, for units that ran
    triage\<accession>\...                      the triage directory the manifest names (section 2.1), byte for byte
                                                except the triage inputs projected below
    decision-ledger.jsonl                       as exported, byte for byte
```

`release-index.json` is `{schema, approved_manifest_digest, manifests: [{name, release, original_sha256,
release_sha256}], triage_inputs: [{name, release, original_sha256, release_sha256, projection}]}`. `name` is the
original's path in the campaign (`campaign-manifest.json`, `units/<unit key>/run-manifest.json`) and `release`
the projection's path in the bundle. `original_sha256` (`sha256:` prefixed, as the approval writes a digest) is
the hash of the original's bytes, which can be checked only on the producing machine. `release_sha256` is the
hash of the projection's bytes. The replay checks the digest from these released bytes as section 3 says.

**Triage inputs.** The Catalog writes a handoff's `file_manifest_path`, `sample_table_path` and
`analysis_input_manifest_path` as resolved drive letter paths, so `inputs\<unit_id>\handoff.json` cannot be
released as it is. The release command handles each file of the triage directory this way:
- A file that holds no absolute path is copied byte for byte.
- A JSON file under an `inputs\` folder that holds one is released at the same path as its release projection
  (below; a path under the workspace root is made relative to the file's own folder, and the Catalog's sidecar
  paths, which lie outside it, become `<external>/<file name>`). `triage_inputs` lists it with `name` and `release` (both
  `triage/<accession>/inputs/...`), `original_sha256` (plain hex, the original bytes), `release_sha256` (the
  released bytes) and `projection: release_projection`.
- A payload, a revision, a decision record, the review log or a file that is not JSON that holds an absolute
  path is refused, and the bundle is not written. Their own bytes are bound by hashes the replay checks, so they
  cannot be projected; they are written again without the path.

The original handoff's sha256 stays verifiable without its bytes. Four released files name it, and the index's
`original_sha256` must equal them:
- the triage's `catalog_handoff_sha256` (released byte for byte, bound by `triage_sha256`);
- the decision record's `catalog_handoff` input `sha256`;
- the hash chained ledger's `triage_recorded` `catalog_handoff_sha256`;
- the manifest's `curated_selection.catalog_handoff_sha256`.

The projection must hash to `release_sha256`, and the replay checks it before it reads the projection
(section 3). The original bytes, with their absolute paths, stay on the producing machine and are not released.
Anyone holding them can check them against `original_sha256`.

The rest of the release is added beside the campaign bundle:
- the skill, prompt templates, schemas, validators, the wrapper and the replay scripts, at their commits;
- the eval set with its versions;
- `replay-report.json`;
- the re-derivation sample and agreement;
- the redacted transcripts of clean configuration runs.

The release command does not write `units\<unit key>\output-checksums.tsv` yet (section 3.3).

**Release projection.** A manifest is released as `<name>.release.json` (`curated.release_projection`):
- the canonical JSON of the manifest;
- every absolute path under the workspace root made relative to the unit's workspace (for the campaign
  manifest, to the campaign directory);
- any other absolute path reduced to `<external>/<file name>`;
- every path to a library the manifest pins replaced by `<library:<file name> sha256=<sha256>>`.

The replay compares the triage files, the canonical hashes and `replay_projection_sha256`, none of which
depends on paths, so the projection loses nothing the replay compares. The one thing it loses is the
manifest digest's recomputation, which only the producing machine can do.

Not released:
- private library content (VS20/VS21 MSP, laboratory LBM2), named only by file name and sha256, never by
  any path;
- raw data, referenced by accession, archive name, size and CRC32;
- finalisation's local only record of original mzTab-M values;
- the original manifests, `campaign-authorization.json` and the campaign's `ledger.sqlite` (with its
  `operator_request` events);
- the original bytes of a triage input released as a projection (named by `original_sha256`);
- transcripts of runs outside the clean configuration.

`scripts/check-release-redaction.py <bundle>` runs over every file of the bundle, redistributed MS-DIAL
outputs included. It refuses:
- a private library name with a path;
- a drive letter path;
- a UNC path (`\\host\share`);
- a user profile path;
- in a transcript, any block the harness inserts from a context file or memory.

The script is still to be built (section 7). Until it exists, `campaign-runner.py release` applies its three path
rules itself (`curated.release_redaction_findings`: drive letter, UNC and user profile paths, as text and as
JSON escapes) to every file it wrote. A bundle with a finding is refused and gets no `release-index.json`, so
the replay never reads it. The bundle it writes therefore passes those three rules.

## 6. Failure handling

- **LLM against header evidence.** After download, Interactive's preflight
  (`msdial_repository_raw_metadata_preflight`, `curated_selection.reconcile_curated_selection`) reconciles
  each inspected file's raw header against the triage's kept row. It compares polarity and acquisition mode
  family (DDA; the DIA family of DIA, SWATH and AIF; MS1 against full scan). MS levels, DIA window signature
  and run end time are not compared.

  The result is `curated_selection_reconciliation`, on the run manifest and on the campaign disposition. It
  holds:
  - `schema`: `msdial-curated-selection-reconciliation.v1`;
  - `triage_sha256` and `analysis_unit_key`;
  - `verdict`: `agree`, `disagree` or `not_evaluated`;
  - `reason`: `triage_header_disagreement` or empty;
  - `inspected`, `agree`, `disagree` and `kept_rows_not_inspected` (member paths);
  - `files`, and `retryable: false`.

  Each item of `files` for a kept row is `{file, member_path, row_index, basis, outcome,
  ion_mode: {triage, triage_basis, header, result}, acquisition_mode: {triage, triage_basis, header, result},
  result, reasons}`. In it:
  - `file` is a portable path;
  - `triage_basis` is `row` or `unit`;
  - each per field `result` is `agree`, `disagree`, `unresolved`, `not_compared` or `not_read`;
  - the item's `result` is `agree` or `disagree`;
  - `reasons` is present only on a disagreement, and holds `polarity`, `acquisition_mode` or
    `header_unread_for_inferred_row`.

  A file in no kept row is `{file, member_path: null, result: "disagree", reason: "not_in_selection",
  reasons: ["not_in_selection"]}`.

  A disagreement is Interactive's disposition hold (`triage_header_disagreement`), which waits for a person
  as every disposition hold does. A unit for which Interactive recorded no reconciliation is not run
  (`contract_held`, `curated_selection_reconciliation_not_recorded`). The runner copies every item, whole,
  into the ledger's `header_reconciliation` entry, with `disagreements_by_field`; there, `polarity` counts as
  `ion_mode`. Nothing goes to the review log.

  Neither side wins silently. Interactive holds the unit until a revised triage is planned again. Its execution
  gate reads the reconciliation again, and a `retry` or `recheck-held` makes the preflight reconcile the same
  headers to the same disagreement, so neither moves the unit. A person's answer reaches the run in one of two
  ways only:
  - **a skip:** a `skip` request ends the unit `skipped` and releases the disposition hold;
  - **a revised triage:** `<unit_id>.r<N>.json`, with its own envelope and a `revision` whose `cause` is that
    ledger entry (`{kind: "header_reconciliation", seq}`). It enters a new plan, and a person approves its own
    manifest digest. To accept the header, the revision changes the row to agree with it; to exclude the file,
    it drops the row.

  Accepting the triage against the header has no path yet: a revision that keeps the row is reconciled to the
  same disagreement. The answer itself reaches the record only as a `person_answer`: a `skip`, `retry` or
  `release-held` request with `--question-code triage_header_disagreement`, `--by` naming them, and their words as
  `--reason`. Direct answers taken by a runner request (accept the header, accept the triage, exclude the file)
  are future work (section 7).
- **Validator error.** The triage is regenerated as `<unit_id>.r<N>.json` with its own envelope; the revision
  entry cites the `validator_verdict` as its cause. The errored payload stays in the bundle with its verdict,
  and the regeneration count per unit is reported.
- **Handoff, record or selection that changed.** Each case stops or holds the unit before or right after
  download:
  - **A triage or record copy that no longer hashes to the manifest**, or a Class decision that is not the
    approved one (`class_digest_changed`), ends the unit `stopped_policy_drift`, with a `terminal_refusal` in
    the ledger.
  - **A Catalog handoff that differs from the triaged one in more than what the Class decides** (section 2.5)
    is held (`contract_held`, `triage_handoff_changed`), as is one whose triaged bytes are not kept. A person
    re-triages the unit or restores the handoff. A difference in the Class alone passes the evidence check: a
    triage made before the Class was saved or ratified is not redone.
  - **A copy whose Class is the approved proposal but is not accepted and ratified under this campaign**, or a
    copy with no `class_proposal` key at all, is held (`contract_held`, `class_not_ratified`); the recheck
    settles and ratifies the Class again and takes the handoff again. A copy carrying another proposal stops the
    unit (`class_digest_changed`, above).
  - **A handoff copy, or a sidecar it names, that no longer hashes to what the handoff step recorded** when the
    download starts is held (`contract_held`, `handoff_copy_changed`), with nothing downloaded; the recheck takes
    the handoff again and checks its Class again.
  - **A `selection_sha256` that differs between the plan and Interactive** is refused by Interactive before
    download, or held by the runner after it (`curated_selection_selection_mismatch`).
- **Gate refusal** (rules P, C, S, D). The units of one download group that carry a curated selection wait
  at a barrier. They go on only once every sibling either has a unit manifest or has ended before its download.
  A sibling held before its download (at its handoff, say) is waited for as well, and the wait records it
  under `held_before_download`. So no sibling reaches the check after another has run, unless it is retried
  after it ended. Then `msdial_repository_cross_unit_check` judges their run manifests once
  (`msdial-cross-unit-attribution.v1`, rule id `cross_unit_attribution_2026_11_16b`, `containment_minimum` 0.5).

  Each unit's admitted Sample inputs are identified by a declared checksum, else by the store object and
  archive member path (`basis: identity`), else by the listed content's CRC32 and size
  (`listed_content_crc32_size`), never by a bare name. A curated row's stated role decides what is compared:
  - a row whose role is `sample` is compared whatever its file name or sample id says, so a biological sample
    named like a QC counts;
  - a row whose role is `qc`, `blank` or `other` is not compared;
  - only a row whose role is `unknown` or empty, or an input no curated row names, is read by its names. QC,
    reference and blank injections found that way (`shared_reference_name`, or the coverage gate's reading)
    are not compared.

  A person's ratified role reaches the check as the row of a revised triage. The rules are applied in order:
  - **P** (`cross_unit_polarity_contradiction`): a header polarity contradicts the unit's.
  - **C** (`cross_unit_inputs_indistinguishable_chromatography`): a set is shared at a containment of 0.5 or
    more across different chromatographies.
  - **S** (`cross_unit_inputs_contained`): at one polarity, and with a chromatography that does not tell the
    units apart, a set is contained at 0.5 or more in a different set. Both units are refused.
  - **D** (`cross_unit_duplicate_inputs`): a set is identical to an earlier unit's; the earlier unit keeps
    it.

  The runner reads the reply by its own keys: `verdict` `passed` or `refused` overall and per unit, the
  codes in `reasons`, and `refused_against`. A reply in any other shape holds the unit (`contract_held`,
  `cross_unit_reply_unreadable`) and is never read as a pass. A group with a single surviving member is not
  sent; the ledger records `not_applicable` with reason `single_member`. So does a curated unit that is the
  only one in its download group, once. A unit that still reaches the check after the group was judged (a
  retry of a unit that ended before its download) is sent after the units an earlier check passed. Rule D
  then refuses the newcomer, never a unit that already ran.

  A refused unit that has not run ends `failed`, terminal reason `attribution_refused`, and is not retried.
  The refusal is sticky. A re-lease of the same workspace and the parts of a split carry it. Only a later
  check that passes the unit over a group holding every unit in `refused_against` lifts it; a check that leaves
  one out reports `prior_refusal_stands`.

  Rules P, C and S can still refuse a unit an earlier check passed. The newcomers are refused with it, and the
  verdict lists the passed unit under `refused_after_pass`. The passed unit is never left silently done:
  - It ends `invalidated_after_done`, a terminal state (ledger schema 5), terminal reason
    `attribution_refused`. A unit already `done` moves there at once. A unit still on its way moves there
    in place of `done`.
  - Its outputs and raw data are kept. The runner never deletes them.
  - The outputs are flagged invalid in four places. In `campaign-record.json`, `outputs_valid` and
    `completed` are false, with an `invalidation` block. The Catalog run record has status
    `invalidated_after_done`. The status export gives `outputs_valid: false`, the report term
    `invalidated_after_done` and a warning, and its summary counts the units. The decision ledger holds a
    `terminal_refusal` with `invalidated_after_done: true` and `outputs: kept_flagged_invalid`.
  - The unit goes to the person queue under the question `cross_unit_refused_after_done`. The person either
    withdraws the outputs from redistribution or re-triages the unit and reopens it (`plan --reopen`; the
    state is reopenable, as `done` is, and is not planned again otherwise).
- **Counts reported.** `scripts/export-llm-decisions.py --summary` writes `supplementary/llm-summary.json`:
  units triaged; units by status; files kept, excluded and abstained; validator errors and holds by check;
  regenerations after a validator error, and the units they affected; person queue size by question code;
  header disagreements by field (`ion_mode`, `acquisition_mode`, `not_in_selection`,
  `header_unread_for_inferred_row`) and by resolution; revisions by cause; cross-unit refusals by rule;
  units invalidated after done, and how each was answered; units withdrawn. The Methods and Figure {x} cite these numbers only.

## 7. Build order

The order a unit goes through is fixed: the triage first, read only and before any Class is saved; then the
plan, whose digest covers the triage and the approved Class digest; then, under the approval, the Class saved and
ratified (boundary 3), the handoff taken, and the download. The triage binds the evidence it read, and the Class
binds by its ratification and the class digest (sections 2.1 and 2.5, WS0 decision F of 2026-11-17, which
supersedes decision A's ratification only scope). No unit is triaged again because its Class was ratified.

Built:
- **Interactive 0.5.69** (PR #102, 0c2921a): `curated_selection`, with the section 3.1 function and the
  listing as its crc32 and size source; the download arguments and checks of section 2.5, with what the Class
  decides left out of the handoff copy check (`HANDOFF_CLASS_KEYS`, `handoff_differing_keys`,
  `handoff_class_split`) and both sides' Class recorded in `original_handoff.class`; reconciliation; the
  cross-unit check, where a stated role overrides the name filter; `replay_projection` v1.
- **The runner** (PR #60, cfb8b39): the plan and runner fields; the run-time copies, with the handoff comparison
  that leaves out what the Class decides (Interactive's `HANDOFF_CLASS_KEYS`, copied and tested equal), the
  Class ratification check at the handoff step (`class_binding`, `class_not_ratified`, `class_digest_changed`), a
  copy with no `class_proposal` key held as `class_not_ratified`, the copy's sha256 checked again at the download
  (`handoff_copy_changed`), and `handoff.triaged.json`; the plan-time workspace name check; the preflight
  comparison of `selection_sha256`; the review log writer and the decision ledger; the cross-unit barrier, `single_member` for a unit alone, and
  `invalidated_after_done`; `campaign-runner.py release`, with projected triage inputs and the three path rules of
  the redaction check; `replay-campaign.py`, with a synthetic fixture test per level and an integration test
  against Interactive.
- **The triage's Class boundary** (PR #60, cfb8b39): the skill never reads or cites `class_proposal`, and
  `validate-triage.py` refuses a triage whose evidence or decision record inputs cite it (`T-CLASS`).

To build:
- schemas and skill 1.1.0;
- `run-triage-agent.py` (clean configuration, envelope from the harness transcript);
- `validate-triage.py` (v2, envelope hashes, projected inputs through the release index, review log answers,
  `--emit-selection`, log append);
- `verify-decision-log.py`, `compare-triage.py`, `export-llm-decisions.py` and `check-release-redaction.py`;
- canonical output hashes at finalisation and `output-checksums.tsv` in the release bundle;
- in Interactive, `parameter_file.canonical_sha256` and projection v2 (next free patch versions).

Future work:
- direct answers to `triage_header_disagreement` taken by a runner request (accept the header, accept the
  triage, exclude the file), in Interactive's reconciliation and execution gate and in the runner; today only a
  skip or a revised triage moves the unit (section 6).
