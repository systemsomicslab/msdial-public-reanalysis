<!--
unit-triage-task.v1: the per-accession task prompt scripts/run-triage-agent.py renders for one triage session
(docs/llm-assisted-curation.md sections 2.3 and 4.1). The skill states the procedure; this prompt states only the
accession, the units, the directories and the boundaries.

Placeholders, each written as its name in double braces below this comment and replaced as plain text:
  accession         the repository accession, e.g. ST002798
  unit_ids          the Catalog analysis unit ids to triage, comma separated, e.g. AN004552, AN004553
  input_dir         the directory holding what the agent may read (handoffs, mwTab text, archive listings)
  output_dir        the directory the agent writes its payloads into
  analysis_purpose  the person's stated analysis_purpose, verbatim, or the word null

Render both directories relative to the session's working directory. The rendered prompt is stored as
inputs\<unit_id>\prompt.md and released byte for byte, so it must hold no absolute path, user name or e-mail
address (section 5.3). The record binds this template by its sha256 at the repository commit and the rendered
prompt by its own sha256. This comment block is part of the template's bytes; the wrapper renders the whole file.
-->
# Unit triage: {{accession}}

Triage accession **{{accession}}** before any download, following the `msdial-unit-triage` skill
(`.claude/skills/msdial-unit-triage/SKILL.md`). Read the skill first and follow it exactly.

## Units

Triage these Catalog analysis units, and only these: {{unit_ids}}.

## Inputs

Everything you may read is in `{{input_dir}}`:

- `<unit_id>/handoff.json` with its `samples.json` and `files.json`: the Catalog handoff of each unit. Read
  the sample rows and files from the sidecars and `handoff.json` only for the unit, the accession and its
  `technical_settings`. Never read or cite its `class_proposal` or its `class_proposal:*` blocking reasons.
- `<analysis id>.mwtab.txt`: the repository's mwTab text of each analysis, where it could be fetched.
- `*.members.tsv`: the member listing of each study archive (archive, path, size, crc32), read from the zip
  central directory before this session.

The Catalog MCP server (`msdial-repository-catalog`) may be read with the tools the skill allows. Nothing
else is evidence: not memory, not the web, not files outside `{{input_dir}}`. If an input you need is
missing, go without it, say so in `evidence_sources`, and ask in `needs_person`.

## Output

Write one `msdial-unit-triage.v2` payload per unit, `{{output_dir}}/<unit_id>.json`, and nothing else.
Write each payload once; do not write a decision record or a review log.

The person's stated analysis purpose (copy it into `purpose` verbatim; `null` means none was stated):
{{analysis_purpose}}

## Boundaries

- Read only: no download, no MS-DIAL run, no Catalog write, no tool call with `confirmed=true`, and no tool
  outside the skill's `allowed-tools`.
- Every decision is a model inference. Give each kept and excluded row and member, and each unit, a
  `confidence` and a `rationale`, and ask rather than guess.
- No absolute path, user name or e-mail address in a payload.

When the payloads are written, report per unit its status, confidence, rows kept and dropped, and the
questions for a person.
