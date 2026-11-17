<!--
unit-triage-revision.v1: the task prompt scripts/run-triage-agent.py --revise renders for one revision session
(docs/llm-assisted-curation.md sections 2.4 and 6). A revision is a new payload <unit_id>.r<N>.json that supersedes
the unit's current payload because of one review-log entry: a person's answer, or a validator verdict. The skill
states the procedure; this prompt states only the accession, the units, their causes, the directories and the
boundaries.

Placeholders, each written as its name in double braces below this comment and replaced as plain text:
  accession         the repository accession, e.g. ST002798
  unit_ids          the Catalog analysis unit ids revised, comma separated
  input_dir         the directory holding what the agent may read (the inputs the current payloads rest on,
                    each current payload and each cause)
  output_dir        the directory the agent writes its revised payloads into
  analysis_purpose  the analysis_purpose, verbatim, or the word null
  handoff_sha256    one line per unit, "- `<unit_id>`: `<sha256>`", the sha256 of the unit's handoff copy
  revisions         one line per unit: the file to write, the payload it revises and the cause entry

The rendered prompt is stored as inputs\<unit_id>\r<N>\prompt.md and released byte for byte, so it must hold no
absolute path, user name or e-mail address. The record binds this template by its sha256 at the repository commit
and the rendered prompt by its own sha256. This comment block is part of the template's bytes.
-->
# Unit triage revision: {{accession}}

Revise the triage of accession **{{accession}}**, following the `msdial-unit-triage` skill
(`.claude/skills/msdial-unit-triage/SKILL.md`), procedure step 10 (Revisions). Read the skill first and follow
it exactly.

## Units and causes

Revise these units, and only these: {{unit_ids}}.

{{revisions}}

Each `previous.json` is the unit's current payload, which stays as it is. Each `cause.json` is one line of the
accession's review log, exactly as it holds it: a `person_answer` (a named person's answer, verbatim, to the
question its `question_code` names) or a `validator_verdict` (the findings that made the payload an error or a
hold). Revise only what the cause settles. A blocking flag may be dropped, or a unit made `ready` over a
question, only where the cause is a person's answer to that code; a person's answer is followed as it is
worded, never widened. Everything else carries over from the previous payload unless the cause shows it wrong.

## Inputs

Everything you may read is in `{{input_dir}}`: the files the current payloads rest on (each unit's
`handoff.json` with its `samples.json` and `files.json` beside it, the `*.mwtab.txt` texts and the
`*.members.tsv` listings), and each revised unit's `r<N>/previous.json` and `r<N>/cause.json`. The paths a
`handoff.json` names for its sidecars point outside this session: never read those paths; read the sidecars
beside it. Never read or cite a `class_proposal` or its `class_proposal:*` blocking reasons. Nothing else is
evidence. A read outside `{{input_dir}}`, the output directory and the skill refuses the whole session.

## Handoff sha256

Write each unit's `catalog_handoff_sha256` exactly as given here:

{{handoff_sha256}}

## Output

Write one `msdial-unit-triage.v2` payload per unit, under the name its line above gives, in
`{{output_dir}}`, and nothing else. Write each once; do not write a decision record or a review log.

The analysis purpose (copy it into `purpose` verbatim; `null` means none was stated):
{{analysis_purpose}}

## Boundaries

- Read only: no download, no MS-DIAL run, no Catalog write, no tool call with `confirmed=true`, and no tool
  outside the skill's `allowed-tools`.
- Every decision is a model inference. Give each kept and excluded row and member, and each unit, a
  `confidence` and a `rationale`, and say in the unit's `rationale` what the cause changed.
- No absolute path, user name or e-mail address in a payload. A person's name in a cause is not copied.

When the payloads are written, report per unit what changed and why.
