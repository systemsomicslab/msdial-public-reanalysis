<!--
unit-triage-revision.v2: the task prompt scripts/run-triage-agent.py --revise renders for one revision session under
skill 1.2.0 (docs/llm-assisted-curation.md sections 2.4 and 6). A revision rewrites the accession's spec as
<accession>.spec.r<N>.json because of review-log entries (a person's answer, or a validator verdict) about named units;
code expands it, and only the named units' payloads may change (X-SCOPE). The skill states the procedure; this prompt
states only the accession, the units, their causes, the directories and the boundaries.

Placeholders, each written as its name in double braces below this comment and replaced as plain text:
  accession         the repository accession, e.g. ST000001
  unit_ids          the Catalog analysis unit ids revised, comma separated
  input_dir         the directory holding what the agent may read (the inputs the current payloads rest on, the
                    current spec, each current payload and each cause)
  output_dir        the directory the agent writes its revised spec into
  spec_name         the revised spec's file name, <accession>.spec.r<N>.json
  previous_spec     the current spec, relative to the working directory
  analysis_purpose  the analysis_purpose, verbatim, or the word null
  revisions         one line per unit: its current payload and the cause entry

The rendered prompt is stored as inputs\<unit_id>\r<N>\prompt.md and released byte for byte, so it must hold no
absolute path, user name or e-mail address. The record binds this template by its sha256 at the repository commit and
the rendered prompt by its own sha256. This comment block is part of the template's bytes.
-->
# Unit triage revision: {{accession}}

Revise the triage of accession **{{accession}}**, following the `msdial-unit-triage` skill
(`.claude/skills/msdial-unit-triage/SKILL.md`; the spec schema it names is `schemas/msdial-unit-triage-spec.v1.json`,
beside it in the working directory), hard rule 9 (Revisions). Read the skill first and follow it exactly.

## Units and causes

Revise these units, and only these: {{unit_ids}}.

{{revisions}}

The current spec is `{{previous_spec}}`. Each `previous.json` is a unit's current payload, which stays as it is. Each
`cause.json` is one line of the accession's review log, exactly as it holds it: a `person_answer` (a named person's
answer, verbatim, to the question its `question_code` names) or a `validator_verdict` (the findings that made the
payload an error or a hold). Revise only what the cause settles. A blocking flag may be dropped, or a unit made `ready`
over a question, only where the cause is a person's answer to that code; a person's answer is followed as it is
worded, never widened. The units not named must expand exactly as before.

## Inputs

Everything you may read is in `{{input_dir}}`: `digest.md`, the files the current payloads rest on, the current spec
and each revised unit's `r<N>/previous.json` and `r<N>/cause.json`. Never follow the paths a `handoff.json` names.
Nothing else is evidence. A read outside `{{input_dir}}`, `{{output_dir}}`, the skill and the schema refuses the whole
session.

The analysis purpose (context only; `null` means none was stated):
{{analysis_purpose}}

## Output

Write the whole spec again as `{{output_dir}}/{{spec_name}}`, and nothing else. Check it with `triage_spec_check` and
fix what it reports with `Edit` until the check reports no error; the check does not compare the units you
leave alone, so keep their groups, flags and questions as the current spec has them. Then reply with the single word
`done`.

## Boundaries

- Read only: no download, no MS-DIAL run, no Catalog write, no tool call with `confirmed=true`, and no tool outside
  the skill's `allowed-tools`.
- Say in each revised unit's `rationale` what the cause changed.
- No absolute path, user name or e-mail address in the spec. A person's name in a cause is not copied.
