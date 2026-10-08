"""The contract names files and gate checks in this repository, and each name must resolve in this tree.

CLAUDE.md, README.md, AGENTS.md and the project skills tell a session which script to run, which
record to read and which template to return. A name that resolves to nothing sends that session
looking for a file that is not there, or lets it conclude that a step does not exist. Locations
outside this repository (the Interactive checkout, the analysis root) are not checked: they are not
this tree's to hold, and they differ from machine to machine.

A document may name a file that another change is bringing, as the campaign amendment names the
campaign runner before it lands. Such a name is listed in PENDING with what brings it, and the test
fails once the file exists, so that the entry is removed rather than left to excuse a later broken
name.

The documents also name gate checks, and the campaign amendment decides by check which
before-production FAILs stop a unit's run (user decisions, 2026-10-01 and 2026-10-02). A check named
there that the gate does not run is a rule nobody applies, and a before-production check the rule
leaves out is one whose FAIL nobody has placed. So every named check must be one the gate runs, the
campaign's gate rule must place every before-production check the gate runs, and the two lists must
hold exactly the checks the trial manifest's latest decision gives them and the gate's RUN_POLICY
states. Since 2026-10-02 the user has placed every check: blocks_run holds ELIG-1, ACQ-1, SUM-1,
CNT-1, INP-1, ID-1, PRE-2 and CONV-1, record_only CLS-1, CLS-2, CLS-3, ORD-1, PKH-1, SPL-1 and PRE-1,
and the paragraph that names the checks in neither list names none. A list may not gain or lose a
check in either direction: one moved to record_only is a FAIL that no longer stops a run, and one
moved to blocks_run is a unit failed and its raw data deleted, and neither is the contract's to decide.

The same decision settled the cases the rule of 2026-10-01 left open, and the contract states each as
decided, with no passage still marked as awaiting the user: a blocks_run check left not_evaluable where
it is required stops the run; a gate that gives no usable report holds the unit, unrun and uncounted
with its raw data kept, and after the run is only recorded; an mzXML without a polarity is given its
unit's one declared ion mode, and CONV-1 stops the unit where there is none; and a stop is the unit's,
never the runner's. The contract states what the runner does under that last rule: a held unit keeps
run --until-idle going for its rechecks; a reply of Interactive's the runner cannot read for one unit, a
reply that does not parse among them, holds that unit (contract_held), as a missing report does, and
pauses nothing; and only what every unit meets alike pauses the campaign, in the four pauses the runner
names, each lifting by itself. Every passage that describes the confirmed=true discard fallback names the
policy field that carries the user's approval of it.

The decision of 2026-10-03 settled three more cases, and the contract states each as decided: a unit
counts as ion mobility only on its own evidence, a mixed unit passing to the raw-header check (option A);
an Interactive backend that does not answer is the fourth pause and a reply the runner cannot read holds
the unit, the two defaults the user did not object to; and an mzXML whose scans mix the opposite polarity
with scans of none is excluded while the rest of its unit runs. The trial manifest records that decision
with the user's answer verbatim. As the review of ba035ca made the runner and the plan hold to it, the
contract also says that the backend pause covers every call, the diagnostic's estimate as much as a job's
poll; that an unreadable poll is never taken for a lost job, the unit waiting for its Console and then held
at that poll; and that a unit whose Catalog record the plan cannot read is never excluded for ion mobility
on the plan's own row.

The decisions of 2026-10-06 to 2026-10-08 are recorded in the trial manifest and quoted in the contract in the
user's words: the lower-end threshold, header-first acquisition (rule B2), inferred pairings on record, AIF by its
collision energies, automatic RT correction, the local production Console, no Catalog recrawl, and merges left to
Claude. PAIR-1 joined record_only on 2026-10-06 as the agent's reading of the pairing decision; the lists are
otherwise those of 2026-10-02. A unit Interactive's disposition holds (disposition_held) is stated as the operator's
skip releases it, only on the user's word. The go signal of 2026-10-01 is the user's.

On 2026-10-08 the user answered the draft's eight open questions in a structured A-D form, and the trial manifest
records the answers as one entry of the user's. Answers 1 to 5 settle what merged code did or the agent had read or
proposed (PAIR-1 in record_only, the unattributed members, the unrecorded-energy AIF hold, the declared-only unit,
the skip that releases a disposition hold): each is stated as the user's decision of 2026-10-08, quoting the chosen
option's Japanese label (and its description where that carries the rule) verbatim from the question, never an
English paraphrase in quotation marks, and each agent entry says the user decided it. Answer 2 is stated as far as
question 2 reached: it did not distinguish a shared archive, so leaving a shared archive's members out is named as
the implementation's rule. Answer 3 replaces the warning-only rule of 2026-09-30, which
the manifest marks as replaced. Answer 6 replaces the hold of differing AIF energy sets with a run on record, which
Interactive #69 and gate #37 implement; the contract no longer states that hold as standing. Answers 7 and 8 revoke
the first pilot's approval and scope its re-run. The contract says, as gate #36 does it, that a --policy override of
the automatic RT correction is stated and covered by the digest rather than refused.

Review then found three points no answer covered, and gate #36 left one choice to the user; the user answered all
four the same day in a second round of the same form, and the trial manifest records them as one more entry of the
user's. Each chosen option's label is quoted exactly as the question showed it, its letter included, and the fourth
answer, given in the user's own words, verbatim, with the agent's reading beside it. The documents state each as
decided and as the open pull requests implement it: a shared archive's members left out of every unit, on record;
each multi-energy AIF input's energy set keyed by its relative path (Interactive #69, gate #37), so Interactive #69
no longer waits and gate #37 merges before or with it; an unpaired mzXML of a unit-scoped archive converted in a
campaign and one of a name's two encodings taken by the encoding order, with what Interactive records for the
members a Catalog declaration does not name stated as it is (nothing of its own); and every manifest without an
automatic RT correction statement refused, to be planned again (gate #36). Review after the second round found
three points of answer 3 that the code does not meet or the answer did not cover: the record of the members a
declaration does not name; the cases the encoding order does not decide, among them a tie with an admitted member
whose record names the order as the basis; and an admitted mzML RawDataHandler cannot decode, replaced by an unpaired
twin that runs outside the sample's Class. Only the passage that states those three is marked open for the user.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import inspect
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = (
    "CLAUDE.md",
    "README.md",
    "AGENTS.md",
    ".claude/skills/msdial-repository-batch/SKILL.md",
    ".claude/skills/msdial-lipidomics-workflow/SKILL.md",
)
_BATCH_SKILL = ".claude/skills/msdial-repository-batch/SKILL.md"
# The top-level folders of this repository. A code span whose first segment is one of them names a
# file here; any other span (a workspace path, a manifest field, an argument) is not a claim about
# this tree.
_REPOSITORY_FOLDERS = frozenset({".claude", "feedback", "prompts", "scripts", "tests", "trials"})
# The documents also name this repository by its absolute location on the author's machine, with
# either separator.
_ABSOLUTE_ROOT = re.compile(r"D:[\\/]13_MSDIAL_Public_Reanalysis[\\/]code[\\/]([^\s`)'\"]+)", re.IGNORECASE)
_FENCE = re.compile(r"^```.*?^```[^\n]*$", re.MULTILINE | re.DOTALL)
# A code span may break across a line, as Markdown lets it, but never across a blank line. Read
# line by line, the span's closing backtick pairs with the next span's opening one, and every name
# after it in the paragraph is read as prose.
_SPAN = re.compile(r"`((?:[^`\n]|\n(?![ \t]*\n))+)`")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
# A file:line reference (scripts/x.py:2760, or :10-20) names the file; Windows would read the
# colon as the start of an alternate data stream name.
_LINE_REFERENCE = re.compile(r":\d+(?:-\d+)?$")

PENDING: dict[str, str] = {}
# Named on purpose and kept out of the repository by .gitignore: audit outputs under feedback/ are
# local by default, and only the review template is version controlled (README).
LOCAL_ONLY = frozenset({"feedback/codex-pre-audit-2026-09-02.md"})

# A gate check id: letters, a hyphen and one digit (SUM-1, CONV-1). Tokens of that shape that are
# not checks are listed here.
_CHECK_ID = re.compile(r"\b[A-Z]{2,5}-[1-9]\b")
_NOT_CHECKS = frozenset({"UTF-8"})
_GATE_RULE_SECTION = "## Gate verdicts in a campaign"
_SCOPE_SECTION = "## Supported production scope"
_GATE_LISTS = ("blocks_run", "record_only")
# The lists as the user gave them on 2026-10-02, written out once more so that a later decision changes
# this test as well as the trial manifest: a list cannot move without the change saying so.
_DECIDED_2026_10_02 = {
    "blocks_run": {"ELIG-1", "ACQ-1", "SUM-1", "CNT-1", "INP-1", "ID-1", "PRE-2", "CONV-1"},
    "record_only": {"CLS-1", "CLS-2", "CLS-3", "ORD-1", "PKH-1", "SPL-1", "PRE-1"},
}
# PAIR-1 came with the user's decision of 2026-10-06 that every inferred name pairing be kept on record. Its place
# in record_only was the agent's reading of that decision (gate #31), and the user decided it on 2026-10-08 (answer
# 1): the trial manifest's entry of that date lists record_only with PAIR-1 in it.
_PLACED_2026_10_06 = {"record_only": {"PAIR-1"}}
_DECIDED_NOW = {name: checks | _PLACED_2026_10_06.get(name, set()) for name, checks in _DECIDED_2026_10_02.items()}
# The paragraph of the gate rule that names the before-production checks the user placed in neither
# list: none since 2026-10-02, and any the gate adds until the user places it.
_NEITHER_LIST = "**In neither list.**"
# What marked a passage as the contract's reading of a case the user had not settled. Since 2026-10-02
# no case is open, and no passage carries it. The discard fallback is marked "awaiting the user's
# approval" instead, and named by its policy field.
_AWAITING = "awaiting the user's decision"
# What each case the user settled on 2026-10-02 is stated as: the lead of the passage that states it, the
# section it is in, and what the passage must say. Each lead is the passage's opening words.
_DECIDED_CASES = {
    "not evaluable": (_GATE_RULE_SECTION, "**Not evaluable.**", (
        "`blocks_run` check left `not_evaluable` where it is", "required stops the run exactly as its FAIL does",
        "`strict_failures`", "not required never stops the run",
    )),
    "no report": (_GATE_RULE_SECTION, "**No report.**", (
        "holds the unit", "keeps its raw data", "counted neither as a retry nor as a failure", "`gate_held`",
        "every other unit goes on", "runs the gate again", "`scripts/campaign-runner.py recheck-held`",
        "a missing report is only recorded",
        # A held unit is not idle, so the rechecks every few hours come by themselves; a request waits for a
        # runner, and the command says when none holds the campaign.
        "`run --until-idle`", "does not return while a unit is held", "one must be started",
    )),
    "a stop is per unit": (_GATE_RULE_SECTION, "**A stop is per unit.**", (
        "never the runner", "pause the whole campaign",
        # The four pauses the runner makes, each lifting by itself, the backend's the user's default of 2026-10-03.
        "A pin change, a short disk, a repository outage and an Interactive backend that does not answer",
        "the four pauses the status export names", "lifts by itself", "did not object to on 2026-10-03",
        "times out", "none of these counts against the unit",
        # Whichever call goes unanswered: the review of ba035ca found the estimate counted against the unit.
        "a job's poll or the diagnostic's estimate",
        # One unit's record holds that unit, a reply that does not parse among them; it pauses nothing.
        "Nothing one unit does pauses the campaign", "`contract_held`", "a reply that does not parse",
        "counted neither as a retry nor as a failure", "leaves that unit's raw data held",
        # An unreadable poll is not a lost job, which started a second production Console on the unit.
        "never taken for a lost job", "keeping the Console slot", "held at that poll",
    )),
    "mzXML without a polarity": (_SCOPE_SECTION, "- **mzXML without a polarity.**", (
        "exactly one polarity", "`technical_settings.ion_mode`", "imputed from that", "as an inference",
        "nothing is imputed", "CONV-1 FAILs", "`blocks_run` check", "no scan of the opposite polarity",
    )),
    # Settled on 2026-10-03.
    "ion mobility from unit-level evidence": (_SCOPE_SECTION, "- **Ion mobility from unit-level evidence.**", (
        "2026-10-03", "option A", "only on its own evidence", "study-level text", "is not such evidence",
        "MTBKS217", "`ion_mobility_evidence` state `enabled`", "A mixed unit", "MTBKS219 and MTBKS220",
        "is not excluded at plan time", "passes to the raw-header check", "split",
        # A record the plan cannot read is no evidence: its own row holds neither rows nor inputs.
        "whose Catalog record the plan cannot read", "never excluded for ion mobility on that row",
    )),
    "mzXML with scans of both polarities": (_SCOPE_SECTION, "- **mzXML with scans of both polarities.**", (
        "did not object to on 2026-10-03", "declares one polarity", "opposite polarity", "record none",
        "excluded with its reason recorded", "the rest of the unit runs", "not given the declared one",
    )),
    # Settled on 2026-10-06 and 2026-10-07.
    "acquisition, header first": (_SCOPE_SECTION, "- **Acquisition, header first (rule B2).**", (
        "2026-10-06", "the header decides", "keyword inference", "not a probability", "is excluded with its reason",
        "not folded into a DDA run", "ACQ-1 FAILs",
    )),
    "AIF": (_SCOPE_SECTION, "- **AIF.**", (
        "2026-10-07 and 2026-10-08", "it runs as SWATH", "MsdialWorkbench #825", "raw data kept",
        "`aif_multi_ce_awaiting_console`", "Interactive #64, #67 and #69 and gate #32, #34 and #37",
    )),
    # Settled on 2026-10-08 (answers 6 and 3).
    "AIF, energy sets that differ": (_SCOPE_SECTION, "- **Energy sets that differ between inputs.**", (
        "2026-10-08", 'answer 6, option C, "そのまま解析する"', "its own per-file representative collision energy",
        'over the hold the agent had recommended (option A, "ユニットごと保留")',
        "`aif_energy_sets_differ_between_inputs`", "WARNs, which stops no run", "Interactive #69", "gate #37",
        "replaces the hold `aif_collision_energies_differ_between_inputs`", "the user had not decided",
        "merged code still holds such a unit", "gate #37 merges first", "taken again by a recheck",
    )),
    # The case Interactive #69 and gate #37 first left open, which question 6 did not cover: the user's second-round
    # answer 2 of 2026-10-08 had both key each input's set by its relative path before #69 merges.
    "AIF, inputs that share a file name": (_SCOPE_SECTION, "- **Inputs that share a file name.**", (
        "share a file name and record different sets", "Question 6 did not cover this case",
        "As first opened", "by its file name alone", "lost its raw data without a run",
        'second-round answer 2, "A: 相対パスで区別するよう直してから使う"', "before #69 merges",
        "`aif_collision_energies_by_input`", "the manifest's `input_directory`", "compared without case",
        "`aif_collision_energies_by_input_key` `path_relative_to_input_directory`", "keeps its basename as its key",
        "looks each input up by the same key", "`POS/QC_01.mzML`", "a record keyed by basename",
        "FAILs ACQ-1 for an input in a subfolder", "Answer 6 then holds for such a unit too",
        "merged Interactive (0.5.35) holds every unit whose inputs record different sets",
    )),
    "AIF, an unrecorded energy": (_SCOPE_SECTION, "- **An input that records no energy.**", (
        "`aif_collision_energy_unrecorded`", "any one input that runs", "holds all forty",
        'answer 3, option A, "ユニットごと保留"', "replaces the user's rule of 2026-09-30",
        "`aif_collision_energy_targets_empty`",
    )),
    "held by Interactive": (_GATE_RULE_SECTION, "**Held by Interactive.**", (
        "`disposition_held`", "raw data kept", "counted neither as a retry nor as a failure",
        "not rechecked by itself", "keeps no runner going", "operator's `skip`", "`operator_skip`",
        "every held part", "only on the user's explicit word", "boundary 5 does not cover it",
        'answer 5, option A, "1件ずつ先生の了承"', "made without asking",
    )),
}
# The user's own words for the decisions of 2026-10-06 to 2026-10-08, which the contract quotes and the trial
# manifest records.
_WORDS_2026_10_06_TO_08 = {
    "2026-10-06": ("High qualityのMS2を取りたい",
                   "必ず記録として残してください"),
    "2026-10-07": ("無理やりにでも",
                   "まだ公開しませんし、大丈夫です",
                   "マージについては、お任せできますか",
                   "12で確認解析を回してください。#826の"
                   "マージもOKです。",
                   "推奨でお願いします"),
    "2026-10-08": ("再クロールは必要ないです！",),
}
# The user's answers of 2026-10-08, each the option the user chose in a structured question the agent put in
# Japanese: the number, the letter, the option's label and description verbatim from the question (None where the
# documents quote the label alone), the English rendering the trial manifest gives beside it, and the section of the
# contract that quotes it. The labels carried a letter prefix and, on option A, the agent's recommendation mark,
# neither of which is quoted.
_ANSWERS_2026_10_08 = {
    1: ("A", "記録だけ", "不一致は必ず記録に残し、そのユニットの解析は続けます。", "record only, the unit runs",
        _GATE_RULE_SECTION),
    2: ("A", "出所不明の入力として含める",
        "対応が取れないファイルも、出所不明の入力として解析に含め、記録に残します。",
        "include them as unattributed inputs, on record", "## Evidence and decisions"),
    3: ("A", "ユニットごと保留", "生データを残して保留にします。", "hold the unit, raw kept", _SCOPE_SECTION),
    4: ("A", "メタデータで解析＋記録",
        "メタデータの取得モードで解析し、ヘッダーが読めなかったことを記録します。",
        "run on the declaration and record it", _SCOPE_SECTION),
    5: ("A", "1件ずつ先生の了承",
        "スキップ（生データ削除）は1件ごとに了承をいただきます。修正後の Console での再判定（recheck）は了承不要です。",
        "the user's OK for each unit; a recheck needs none", _GATE_RULE_SECTION),
    6: ("C", "そのまま解析する", "ファイルごとの代表 CE でそのまま解析し、CE がそろっていないことを記録します。",
        "run as is", _SCOPE_SECTION),
    7: ("A", "取り消してよい", None, "revoke it", "## Confirmation boundaries"),
    8: ("A", "未完了の10ユニット",
        "前回完了しなかった10ユニットを新しい構成（自動 RT 補正 ON）で回します。",
        "its 10 unfinished units", "## Confirmation boundaries"),
}
# Question 2 verbatim: it names ST001264 and does not distinguish a unit's own archive from a shared one.
_QUESTION_2 = "ST001264 のように、アーカイブ内のファイルの一部しかサンプル表と対応が取れない場合、どうしますか？"
# The English renderings an earlier draft put in quotation marks as if they were the options' words.
_PARAPHRASES_QUOTED = ('"record only, the unit runs"', '"include them as unattributed inputs, on record"',
                       '"hold the unit, raw kept"', '"run on the declaration and record it"',
                       '"the user\'s OK for each unit; a recheck needs none"', '"run as is"', '"yes"',
                       '"the 10 unfinished units"')
# The user's request for that form, verbatim.
_A_TO_D_REQUEST = "すみません、判断をしないといけない点に関して、A～Dあたりの質問形式で、順番に出してもらえますか？"
# What marks a passage as a case the user has not settled. After the second round of 2026-10-08 one passage carries
# it: the three points of answer 3 that review found the code does not meet or the answer did not cover.
_OPEN_MARKS = ("open for the user", "not yet put to the user", "not yet accepted", "has not been asked",
               "the user did not decide", "stands as the user's")
# The user's second-round answers of 2026-10-08, each: the label exactly as the question showed it (letter included),
# or for answer 4 the user's own words, and the passage of the contract that states what it decides (its section and
# its lead).
_SECOND_ROUND = {
    1: "A: 除外して記録する",
    2: "A: 相対パスで区別するよう直してから使う",
    3: "A: mzXMLは変換、同名2形式は1つ選ぶ",
    4: "なんのことかわからないのですが、デフォルトでは補正ON、ということで良いんじゃないですか？",
}
_SECOND_ROUND_HEADING = "**The second round.**"
# The passage after the second round's list that leaves three points of answer 3 to the user, and its three items.
_SECOND_ROUND_UNSETTLED = "Review after the second round found three points of answer 3"
_SECOND_ROUND_UNSETTLED_ITEMS = (
    "- **The record of the members a Catalog declaration does not name.**",
    "- **The cases the encoding order does not decide.**",
    "- **An admitted encoding RawDataHandler cannot decode.**",
)
# The heads of the pull requests that implement the second round, as the documents cite them.
_SECOND_ROUND_HEADS = {"Interactive #69": "4722776", "gate #37": "c716368", "gate #36": "f043ab1"}
# The reasons Interactive records for the members it leaves out of a unit-scoped archive, and the records it keeps of
# what it took, after the second round's answer 3 (Interactive #69).
_UNIT_SCOPED_LEFT_OUT = ("`requires_conversion`", "`polarity_token_contradicts_ion_mode`",
                         "`two_encodings_of_one_name`", "`download_scope_not_unit_scoped`",
                         "`unattributed_members.left_out`", "`chosen_other_encoding`",
                         "`unattributed_members.converted`", "`shared_archive`")
# What the documents stated while the shared-file-name point was open, and no longer state: the interim, the naming
# step a review found could not be carried out, and #69 held back from the delegation of merges.
_WITHDRAWN = ("states this case and what it costs", "the agent's precaution",
              "name every unit whose inputs share a file name", "names to the user, before a manifest is approved",
              "Interactive #69 is not merged", "answer 6 takes effect for no unit",
              "a manifest approval is not an answer to this point", "**Open points.**", "Open points)")
# What the trial manifest's 2026-10-03 decision says, and the user's answer verbatim.
_DECISION_2026_10_03_SAYS = (
    "option A", "unit-level evidence", "is not such evidence", "MTBKS217", "MTBKS219 and MTBKS220",
    "15 named analysis units, raw data kept", "a fourth pause of the whole campaign",
    "cannot read holds the unit", "mix the opposite polarity with scans of no polarity is excluded",
    "\u6848A\u3067OK\u3001\u307e\u305f\u30d1\u30a4\u30ed\u30c3\u30c8\u958b\u59cb\u3082"
    "\u30b9\u30bf\u30fc\u30c8\u3057\u3066\u304f\u3060\u3055\u3044\u3002",
)
# What the trial manifest's 2026-10-02 decision says of the same cases, and the user's answer verbatim.
_DECISION_SAYS = (
    "not_evaluable where it is required", "no report", "holds the unit", "technical_settings.ion_mode",
    "'Stop' is per unit", "\u306f\u3044\u3001\u63d0\u6848\u901a\u308a\u3067\u826f\u3044\u3067\u3059",
)
_DISCARD_FALLBACK_FIELD = "confirmed_discard_fallback"
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*]|\d+\.)[ \t]")


def _normalised(token: str) -> str:
    name = token.replace("\\", "/").rstrip(".,;")
    if name.startswith("./"):
        name = name[2:]
    return _LINE_REFERENCE.sub("", name).rstrip(":")


def _named_paths(text: str) -> set[str]:
    """Repository-relative paths a document names: in code spans, Markdown links and absolute locations."""
    names = {match.group(1) for match in _ABSOLUTE_ROOT.finditer(text)}
    # A fenced block is removed before spans are paired: its three backticks would otherwise shift
    # every pairing after it, and the spans read would be the prose between them.
    prose = _FENCE.sub("", text)
    tokens = [words[0] for words in (match.group(1).split() for match in _SPAN.finditer(prose)) if words]
    tokens += [match.group(1) for match in _LINK.finditer(prose)]
    for token in tokens:
        token = _normalised(token)
        if token.split("/", 1)[0] in _REPOSITORY_FOLDERS:
            names.add(token)
    cleaned = set()
    for name in names:
        name = _normalised(name)
        if not any(mark in name for mark in "<>*"):
            cleaned.add(name)
    return cleaned


def _exists_exactly(root: Path, name: str) -> bool:
    """Whether the path exists here with this exact spelling. NTFS matches any case; Linux does not."""
    here = root
    for part in [item for item in name.split("/") if item]:
        try:
            entries = os.listdir(here)
        except OSError:
            return False
        if part not in entries:
            return False
        here = here / part
    return True


@functools.cache
def _verifier():
    path = _ROOT / "scripts" / "verify-run-invariants.py"
    spec = importlib.util.spec_from_file_location("verify_run_invariants_contract", path)
    assert spec and spec.loader
    verifier = importlib.util.module_from_spec(spec)
    sys.modules["verify_run_invariants_contract"] = verifier
    spec.loader.exec_module(verifier)
    return verifier


def _gate_checks_by_stage() -> dict[str, list[str]]:
    """The check ids the gate runs at each stage, read from the gate itself on an empty workspace.

    Every check reports on a workspace with no artifacts (not_evaluable, never skipped), so this is
    the gate's own list, not one written down beside it.
    """
    verifier = _verifier()
    with tempfile.TemporaryDirectory() as scratch:
        return {stage: [check.check_id for check in verifier.verify(Path(scratch), stage).checks]
                for stage in verifier.STAGES}


def _gate_run_policy() -> dict[str, set[str]]:
    """The two lists as the gate's RUN_POLICY states them, the table its --json run_policy is read from."""
    verifier = _verifier()
    return {name: {check for check, rule in verifier.RUN_POLICY.items() if rule == name} for name in _GATE_LISTS}


def _check_ids(text: str) -> set[str]:
    return set(_CHECK_ID.findall(text)) - _NOT_CHECKS


def _blocks(text: str) -> list[str]:
    """A document's paragraphs, list items and headings, each with its whitespace collapsed."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip() or _LIST_ITEM.match(line) or line.startswith("#"):
            if current:
                blocks.append(current)
            current = [line] if line.strip() else []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return [" ".join(" ".join(block).split()) for block in blocks]


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    following = text.find("\n## ", start + len(heading))
    return text[start:] if following < 0 else text[start:following]


def _gate_list(section: str, name: str) -> set[str]:
    """The checks a `- `name`:` bullet of the gate rule names, through its continuation lines."""
    lines = section.splitlines()
    marker = f"- `{name}`:"
    starts = [index for index, line in enumerate(lines) if line.startswith(marker)]
    if len(starts) != 1:
        raise AssertionError(f"the gate rule has {len(starts)} bullets for {name}, not one")
    bullet = [lines[starts[0]]]
    for line in lines[starts[0] + 1:]:
        if not line.startswith("  "):
            break
        bullet.append(line)
    return _check_ids("\n".join(bullet))


def _parenthesised_lists(text: str, name: str) -> list[set[str]]:
    """Each list a passage gives in parentheses after "a `name` check", as the batch skill's step 12 does. A
    parenthesis that names no check (a date, a reference) is no list."""
    found = [_check_ids(match.group(1)) for match in re.finditer(rf"`{name}` check\s*\(([^)]*)\)", text)]
    return [checks for checks in found if checks]


def _decided_lists(decisions: list[dict]) -> dict[str, set[str]]:
    """For each list, the checks named by the latest decision sentence that puts checks in it."""
    found: dict[str, set[str]] = {}
    for entry in decisions:
        for sentence in re.split(r"(?<=[.])\s+", str(entry.get("decision") or "")):
            for name in _GATE_LISTS:
                if f"({name})" in sentence:
                    found[name] = _check_ids(sentence)
    return found


def _latest_list_decision(decisions: list[dict]) -> dict:
    """The latest decision that puts checks in a list."""
    return [entry for entry in decisions if any(f"({name})" in str(entry.get("decision") or "") for name in _GATE_LISTS)][-1]


def _trial_decisions() -> list[dict]:
    return json.loads(
        (_ROOT / "trials" / "2026-09-20-ten-unit-trial-manifest.json").read_text(encoding="utf-8")
    )["decisions"]


def _second_round_entry() -> dict:
    """The trial manifest's one entry of the user's second-round answers of 2026-10-08."""
    entries = [entry for entry in _trial_decisions()
               if entry.get("at") == "2026-10-08" and "Second-round answer 1" in str(entry.get("decision"))]
    assert len(entries) == 1, len(entries)
    return entries[0]


def _gate_rule_disagreements(
    contract: str, before_production: set[str], decided: dict[str, set[str]],
) -> list[str]:
    """Where the campaign's gate rule departs from the gate or from the user's decision; empty if nowhere."""
    problems = []
    rule = _section(contract, _GATE_RULE_SECTION)
    named = _check_ids(rule)
    if named != before_production:
        problems.append(f"the rule names {sorted(named - before_production)} beyond the gate's "
                        f"before-production checks and leaves {sorted(before_production - named)} unnamed")
    lists = {}
    for name in _GATE_LISTS:
        try:
            lists[name] = _gate_list(rule, name)
        except AssertionError as error:
            problems.append(str(error))
            continue
        given = decided.get(name, set())
        if lists[name] != given:
            problems.append(f"{name} holds {sorted(lists[name] - given)} the user did not give it and "
                            f"lacks {sorted(given - lists[name])} the user did")
    if len(lists) == len(_GATE_LISTS) and lists["blocks_run"] & lists["record_only"]:
        problems.append(f"{sorted(lists['blocks_run'] & lists['record_only'])} are in both lists")
    unplaced = before_production - set().union(*decided.values())
    paragraphs = [item for item in re.split(r"\n[ \t]*\n", rule)
                  if item.lstrip().startswith(_NEITHER_LIST)]
    if len(paragraphs) != 1:
        problems.append(f"the rule has {len(paragraphs)} '{_NEITHER_LIST}' paragraphs, not one")
    elif _check_ids(paragraphs[0]) != unplaced:
        problems.append(f"'{_NEITHER_LIST}' names {sorted(_check_ids(paragraphs[0]))}, and the checks "
                        f"the user placed in neither list are {sorted(unplaced)}")
    return problems


def _moved(contract: str, check: str, source: str, target: str) -> str:
    """The contract with `check` taken from the `source` bullet of the gate rule and put at the head of
    the `target` one."""
    rule = _section(contract, _GATE_RULE_SECTION)
    lines = rule.split("\n")
    start = next(index for index, line in enumerate(lines) if line.startswith(f"- `{source}`:"))
    end = start + 1
    while end < len(lines) and lines[end].startswith("  "):
        end += 1
    bullet = "\n".join(lines[start:end])
    taken = re.sub(rf"(?:, | and | or ){re.escape(check)}\b|\b{re.escape(check)}(?:, | and )", "", bullet, count=1)
    assert taken != bullet, f"{check} is not in the {source} bullet"
    changed = rule.replace(bullet, taken, 1)
    marker = f"- `{target}`: "
    at = changed.index(marker) + len(marker)
    changed = changed[:at] + check + ", " + changed[at:]
    return contract.replace(rule, changed, 1)


def _blocks(text: str) -> list[str]:
    """A document's paragraphs, each list item on its own and on one line, with fenced blocks left out."""
    blocks = []
    for paragraph in re.split(r"\n[ \t]*\n", _FENCE.sub("", text)):
        item: list[str] = []
        for line in paragraph.splitlines():
            if _LIST_ITEM.match(line) and item:
                blocks.append(" ".join(" ".join(item).split()))
                item = []
            item.append(line)
        if item:
            blocks.append(" ".join(" ".join(item).split()))
    return blocks


def _case_problems(contract: str) -> list[str]:
    """Where the contract does not state a case the user settled (2026-10-02, 2026-10-03) as decided; empty if
    nowhere."""
    problems = []
    for case, (heading, lead, phrases) in _DECIDED_CASES.items():
        passages = [block for block in _blocks(_section(contract, heading)) if block.startswith(lead)]
        if len(passages) != 1:
            problems.append(f"{case}: {len(passages)} passages open with {lead!r}, not one")
            continue
        missing = [phrase for phrase in phrases if " ".join(phrase.split()) not in passages[0]]
        if missing:
            problems.append(f"{case}: the passage does not say {missing}")
    return problems


def _still_awaiting(text: str) -> list[str]:
    """Passages that still mark a case as awaiting the user's decision."""
    return [block[:160] for block in _blocks(text) if _AWAITING in block.casefold()]


def _fallback_without_its_field(text: str) -> list[str]:
    """Passages that describe the confirmed=true discard fallback without the policy field that allows it."""
    return [block[:160] for block in _blocks(text)
            if "fallback" in block and "confirmed=true" in block and _DISCARD_FALLBACK_FIELD not in block]


class ContractReferencesTests(unittest.TestCase):
    def test_every_repository_path_the_contract_names_exists(self) -> None:
        missing = {}
        for document in DOCUMENTS:
            text = (_ROOT / document).read_text(encoding="utf-8")
            for name in _named_paths(text):
                if name not in PENDING and name not in LOCAL_ONLY and not _exists_exactly(_ROOT, name):
                    missing.setdefault(document, []).append(name)
        self.assertEqual({}, missing)

    def test_a_pending_path_is_still_named_and_not_yet_present(self) -> None:
        named = set()
        for document in DOCUMENTS:
            named |= _named_paths((_ROOT / document).read_text(encoding="utf-8"))
        for name, brought_by in PENDING.items():
            with self.subTest(name=name):
                self.assertIn(name, named, f"{name} is no longer named; drop it from PENDING")
                self.assertFalse(
                    (_ROOT / name).exists(),
                    f"{name} exists now ({brought_by} landed); drop it from PENDING",
                )

    def test_the_reader_finds_names_after_a_fenced_block(self) -> None:
        text = "```text\nD:\\x\n```\n\nRun `scripts/one.py --flag`, then read `trials/two.json`.\n"
        self.assertEqual({"scripts/one.py", "trials/two.json"}, _named_paths(text))

    def test_the_reader_maps_the_absolute_location_and_skips_placeholders(self) -> None:
        text = (
            "Follow `D:\\13_MSDIAL_Public_Reanalysis\\code\\prompts\\01-audit.md`.\n"
            "```powershell\npython D:\\13_MSDIAL_Public_Reanalysis\\code\\scripts\\gate.py `\n```\n"
            "Write `<workspace>\\output\\x.csv` and `tests/<name>.py`, never `output\\method.txt`.\n"
        )
        self.assertEqual({"prompts/01-audit.md", "scripts/gate.py"}, _named_paths(text))

    def test_the_reader_finds_the_names_a_review_showed_it_missing(self) -> None:
        text = (
            "Run `./scripts/one.py`, read [the trial](trials/two.json) and\n"
            "`D:/13_MSDIAL_Public_Reanalysis/code/prompts/three.md`.\n"
        )
        self.assertEqual({"scripts/one.py", "trials/two.json", "prompts/three.md"}, _named_paths(text))

    def test_a_span_broken_across_a_line_keeps_the_names_after_it(self) -> None:
        text = "Record it with `--digest <d> --by <p>\n--conclusion accepted`, then run `scripts/four.py`.\n"
        self.assertEqual({"scripts/four.py"}, _named_paths(text))

    def test_a_line_reference_names_its_file(self) -> None:
        text = "See `scripts/verify-run-invariants.py:2760` and `scripts/record-reading.py:10-20`.\n"
        names = _named_paths(text)
        self.assertEqual({"scripts/verify-run-invariants.py", "scripts/record-reading.py"}, names)
        self.assertTrue(all(_exists_exactly(_ROOT, name) for name in names))

    def test_a_name_resolves_only_in_its_own_case(self) -> None:
        self.assertTrue(_exists_exactly(_ROOT, "scripts/record-reading.py"))
        self.assertFalse(_exists_exactly(_ROOT, "scripts/Record-Reading.PY"))
        self.assertFalse(_exists_exactly(_ROOT, "Scripts/record-reading.py"))


class ContractGateChecksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stages = _gate_checks_by_stage()
        cls.contract = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    def test_every_check_the_documents_name_is_one_the_gate_runs(self) -> None:
        runs = {check for checks in self.stages.values() for check in checks}
        unknown = {}
        for document in DOCUMENTS:
            named = _check_ids((_ROOT / document).read_text(encoding="utf-8")) - runs
            if named:
                unknown[document] = sorted(named)
        self.assertEqual({}, unknown)

    def _decided(self) -> dict[str, set[str]]:
        decided = _decided_lists(_trial_decisions())
        self.assertEqual(set(_GATE_LISTS), set(decided), "the trial manifest records no decision for a list")
        self.assertTrue(all(decided.values()))
        return decided

    def test_the_campaign_gate_rule_places_every_before_production_check(self) -> None:
        rule = _section(self.contract, _GATE_RULE_SECTION)
        self.assertEqual(set(self.stages["before-production"]), _check_ids(rule))

    def test_the_two_lists_hold_exactly_the_checks_the_user_gave_them(self) -> None:
        rule = _section(self.contract, _GATE_RULE_SECTION)
        lists = {name: _gate_list(rule, name) for name in _GATE_LISTS}
        self.assertFalse(lists["blocks_run"] & lists["record_only"], "a check is in both lists")
        decided = self._decided()
        gate = _gate_run_policy()
        for name in _GATE_LISTS:
            with self.subTest(list=name):
                self.assertEqual(decided[name], lists[name], "the contract's list is the latest decision's")
                self.assertEqual(gate[name], lists[name], "the contract's list is the gate's RUN_POLICY's")

    def test_the_lists_are_the_ones_the_user_gave_on_2026_10_02(self) -> None:
        # With PAIR-1, placed on 2026-10-06 as the reading of the user's pairing decision and decided by the user on
        # 2026-10-08, and nothing else.
        self.assertEqual(_DECIDED_NOW, self._decided())
        self.assertEqual(_DECIDED_NOW, _gate_run_policy())
        self.assertEqual(set(self.stages["before-production"]), set().union(*_DECIDED_NOW.values()),
                         "every before-production check the gate runs is placed")
        entry = _latest_list_decision(_trial_decisions())
        self.assertEqual("2026-10-08", entry["at"])
        self.assertTrue(str(entry["by"]).startswith("user"), "PAIR-1's place is the user's since 2026-10-08")
        self.assertIn("Answer 1", str(entry["decision"]))

    def test_pair1s_place_is_stated_as_the_users_decision_of_2026_10_08(self) -> None:
        rule = " ".join(_section(self.contract, _GATE_RULE_SECTION).split())
        for phrase in ("PAIR-1 came with the decision of 2026-10-06", "the agent's reading of that decision",
                       'the user decided that place on 2026-10-08 (answer 1, option A, "記録だけ"'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, rule)
        self.assertNotIn("has not been asked to confirm", rule)

    def test_the_batch_skill_gives_the_same_lists(self) -> None:
        skill = (_ROOT / _BATCH_SKILL).read_text(encoding="utf-8")
        decided = self._decided()
        for name in _GATE_LISTS:
            with self.subTest(list=name):
                given = _parenthesised_lists(skill, name)
                self.assertTrue(given, f"the batch skill gives no {name} list")
                self.assertEqual([decided[name]] * len(given), given)

    def test_the_checks_the_user_placed_in_neither_list_are_named_as_such(self) -> None:
        before_production = set(self.stages["before-production"])
        self.assertEqual([], _gate_rule_disagreements(self.contract, before_production, self._decided()))

    def test_a_check_moved_between_the_lists_is_refused(self) -> None:
        before_production = set(self.stages["before-production"])
        decided = self._decided()
        # Moved to record_only, its FAIL would stop no run; moved to blocks_run, each FAIL would fail the
        # unit and delete its raw data. The user decided neither.
        for check, source, target in (("CONV-1", "blocks_run", "record_only"), ("ID-1", "blocks_run", "record_only"),
                                      ("SPL-1", "record_only", "blocks_run"), ("PKH-1", "record_only", "blocks_run")):
            with self.subTest(check=check, to=target):
                moved = _moved(self.contract, check, source, target)
                self.assertEqual(before_production, _check_ids(_section(moved, _GATE_RULE_SECTION)))
                problems = _gate_rule_disagreements(moved, before_production, decided)
                self.assertTrue(any(item.startswith(f"{target} holds ['{check}']") for item in problems), problems)
                self.assertTrue(any(item.startswith(f"{source} holds [] ") for item in problems), problems)

    def test_pre1_reports_no_fail_for_the_rule_to_place(self) -> None:
        # The rule says PRE-1 never FAILs. It is reported in one function, which names no FAIL.
        verifier = _verifier()
        function = inspect.getsource(verifier.check_preflight_claim)
        reported = re.compile(r'\.add\(\s*"PRE-1"')
        self.assertTrue(reported.findall(function))
        self.assertEqual(len(reported.findall(inspect.getsource(verifier))), len(reported.findall(function)))
        self.assertEqual({"PASS", "WARN", "NOT_EVALUABLE"},
                         set(re.findall(r"\b(PASS|WARN|FAIL|NOT_EVALUABLE)\b", function)))

    def test_a_record_only_fail_hides_an_unevaluated_blocks_run_check_from_the_exit_code(self) -> None:
        # The rule says the runner reads the report, not the exit code, because a FAIL outranks a strict
        # refusal. A production bundle with no peak-height diagnostic FAILs PKH-1 (record_only), and the
        # blocks_run checks whose artifacts are missing are left not_evaluable beside it: exit 2, not 4.
        # Those stop the run all the same (2026-10-02), and the report's run_blocked_by names them.
        verifier = _verifier()
        decided = self._decided()
        with tempfile.TemporaryDirectory() as scratch:
            unit = Path(scratch) / "unit"
            (unit / "provenance").mkdir(parents=True)
            (unit / "output").mkdir()
            (unit / "provenance" / "run-manifest.json").write_text(json.dumps({
                "schema": "msdial-public-reanalysis-run.v1",
                "project": {"analysis_unit_id": "unit"},
                "execution_allowed": True,
            }), encoding="utf-8")
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = verifier.main([str(unit), "--stage", "before-production", "--strict", "--json"])
        report = json.loads(printed.getvalue())
        fails = {check["check_id"] for check in report["checks"] if check["status"] == verifier.FAIL}
        self.assertEqual(2, code)
        self.assertTrue(fails)
        self.assertLessEqual(fails, decided["record_only"])
        unevaluated = set(report["strict_failures"]) & decided["blocks_run"]
        self.assertTrue(unevaluated, report["strict_failures"])
        self.assertEqual(unevaluated, set(report["run_blocked_by"]), "the unevaluated blocks_run checks stop the run")

    def test_the_rule_is_refused_where_it_departs_from_the_gate_or_the_user(self) -> None:
        section = (
            "## Gate verdicts in a campaign\n\n"
            "- `blocks_run`: ELIG-1 and\n  ACQ-1. Such a unit fails.\n"
            "- `record_only`: CLS-1.\n\n"
            f"{_NEITHER_LIST} SUM-1, and nothing else.\n\n"
            "The runner reads the report.\n"
        )
        gate = {"ELIG-1", "ACQ-1", "CLS-1", "SUM-1"}
        decided = {"blocks_run": {"ELIG-1", "ACQ-1"}, "record_only": {"CLS-1"}}
        self.assertEqual([], _gate_rule_disagreements(section, gate, decided))
        cases = {
            "a check the gate added": (section, gate | {"CNT-1"}, decided),
            "a decided check dropped": (section.replace("ELIG-1 and\n  ", ""), gate, decided),
            "the paragraph deleted": (section.replace(f"{_NEITHER_LIST} SUM-1, and nothing else.\n\n", ""), gate, decided),
            "an unplaced check in a list": (section.replace("- `record_only`: CLS-1.", "- `record_only`: SUM-1, CLS-1."),
                                            gate, decided),
            "a placed check called unplaced": (section.replace("SUM-1, and", "SUM-1 and CLS-1, and"),
                                               gate, decided),
            "a later decision the rule missed": (section, gate,
                                                 {**decided, "record_only": {"CLS-1", "SUM-1"}}),
        }
        for case, (text, checks, lists) in cases.items():
            with self.subTest(case=case):
                self.assertTrue(_gate_rule_disagreements(text, checks, lists))

    def test_every_case_the_user_settled_is_stated_as_decided(self) -> None:
        self.assertEqual([], _case_problems(self.contract))

    def test_no_document_marks_a_case_as_still_awaiting_the_user(self) -> None:
        found = {}
        for document in DOCUMENTS:
            text = (_ROOT / document).read_text(encoding="utf-8")
            if _still_awaiting(text):
                found[document] = _still_awaiting(text)
        self.assertEqual({}, found)

    def test_a_case_stated_as_the_draft_read_it_is_refused(self) -> None:
        # Before 2026-10-02 the draft read the open cases otherwise: a gate with no report stopped the run as a
        # failed attempt, a check left not_evaluable was recorded whatever its list, and an mzXML's polarity
        # was never imputed. Each reading put back is refused, and so is a passage put back under its mark.
        readings = {
            "no report": ("holds the unit", "stops the run"),
            "not evaluable": ("required stops the run exactly as its FAIL does", "is recorded with the unit"),
            "mzXML without a polarity": ("exactly one polarity", "no polarity at all"),
            "a stop is per unit": ("never the runner", "and the campaign stops"),
        }
        for case, (decided, earlier) in readings.items():
            with self.subTest(case=case):
                heading, lead, _phrases = _DECIDED_CASES[case]
                self.assertIn(decided, " ".join(_section(self.contract, heading).split()))
                section = _section(self.contract, heading)
                earlier_section = re.sub(r"\s+".join(map(re.escape, decided.split())), earlier, section)
                self.assertNotEqual(section, earlier_section)
                problems = _case_problems(self.contract.replace(section, earlier_section))
                self.assertTrue(any(item.startswith(f"{case}:") for item in problems), problems)
        marked = self.contract.replace("**No report.**", f"**No report, {_AWAITING}.**")
        self.assertTrue(_still_awaiting(marked))
        self.assertTrue(any(item.startswith("no report:") for item in _case_problems(marked)))

    def test_the_latest_decision_settles_the_cases_in_the_users_words(self) -> None:
        # The user's decision of 2026-10-02 that placed every check then run; 2026-10-08 added only PAIR-1's place.
        decision = str(_latest_list_decision([entry for entry in _trial_decisions()
                                              if str(entry.get("by", "")).startswith("user")
                                              and str(entry.get("at", "")).startswith("2026-10-02")])["decision"])
        for phrase in _DECISION_SAYS:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, decision)

    def test_the_2026_10_03_decision_is_recorded_in_the_users_words(self) -> None:
        entries = [entry for entry in _trial_decisions() if str(entry.get("at", "")).startswith("2026-10-03")]
        self.assertEqual(1, len(entries))
        decision = str(entries[0]["decision"])
        for phrase in _DECISION_2026_10_03_SAYS:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, decision)
        self.assertFalse(any(f"({name})" in decision for name in _GATE_LISTS), "it moves no check between the lists")

    def test_the_decisions_of_2026_10_06_to_08_are_recorded_and_quoted_in_the_users_words(self) -> None:
        decisions = _trial_decisions()
        for day, words in _WORDS_2026_10_06_TO_08.items():
            entries = [entry for entry in decisions
                       if str(entry.get("at", "")).startswith(day) and str(entry.get("by", "")).startswith("user")]
            self.assertTrue(entries, day)
            for phrase in words:
                with self.subTest(day=day, phrase=phrase):
                    self.assertEqual(1, len([entry for entry in entries if phrase in str(entry["decision"])]))
                    self.assertIn(phrase, self.contract)

    def test_the_rule_the_user_replaced_is_not_stated_as_standing(self) -> None:
        """On 2026-10-06 the user merged the pull requests; on 2026-10-07 the user left merges to Claude. The
        approval-taking discard has been on Interactive main since 0.5.22."""
        merges = " ".join(_section(self.contract, "## Merges").split())
        self.assertIn("replaces the rule of 2026-10-06 that the user merges", merges)
        self.assertIn("still takes the user's explicit OK", merges)
        self.assertNotIn("It is not on Interactive main yet", self.contract)

    def test_the_answers_of_2026_10_08_are_the_users_and_recorded_as_chosen(self) -> None:
        """The user answered in a structured question form, which the user asked for; the trial manifest records each
        answer's number, letter and option, and the contract quotes each chosen option's label (and description)
        verbatim from the Japanese question, never an English rendering in quotation marks."""
        entries = [entry for entry in _trial_decisions()
                   if entry.get("at") == "2026-10-08" and _A_TO_D_REQUEST in str(entry["decision"])]
        self.assertEqual(1, len(entries))
        self.assertTrue(str(entries[0]["by"]).startswith("user"))
        decision = str(entries[0]["decision"])
        for phrase in ("in a structured question form the user asked for", "two or three options lettered from A",
                       "marked option A of every question as its recommendation",
                       "option A for seven questions and option C for question 6", "the agent's rendering"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, decision)
        self.assertNotIn("options A to D", decision)
        contract = " ".join(self.contract.split())
        self.assertIn(_A_TO_D_REQUEST, contract)
        for phrase in ("two or three options lettered from A",
                       "marked option A of every question as its recommendation",
                       "for question 6 chose option C over it", "verbatim from the question",
                       "English beside such a quote is the agent's rendering, not a quote"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, contract)
        self.assertNotIn("options A to D", contract)
        for number, (letter, label, description, rendering, heading) in _ANSWERS_2026_10_08.items():
            with self.subTest(answer=number):
                self.assertRegex(decision, rf"Answer {number}, [^.]*option {letter}, {re.escape(label)}")
                self.assertIn(f"({rendering}", decision)
                section = " ".join(_section(self.contract, heading).split())
                quoted = f'answer {number}, option {letter}, "{label}"'
                self.assertIn(quoted + (f': "{description}"' if description else ""), section)
                if description:
                    self.assertIn(f"{label}: {description}", decision)
        for document in DOCUMENTS:
            text = " ".join((_ROOT / document).read_text(encoding="utf-8").split())
            for paraphrase in _PARAPHRASES_QUOTED:
                with self.subTest(document=document, paraphrase=paraphrase):
                    self.assertIsNone(re.search(r"option [A-D][:,] " + re.escape(paraphrase), text))

    def test_answer_2_is_stated_only_as_far_as_question_2_reached(self) -> None:
        """Question 2 named ST001264 and did not distinguish a unit's own archive from a shared one; leaving a shared
        archive's members out was the implementation's rule until the user's second-round answer 1 decided it, and no
        document narrows question 2 to say otherwise."""
        section = " ".join(_section(self.contract, "## Evidence and decisions").split())
        for phrase in (_QUESTION_2, "the user accepted it on 2026-10-08 so far as the question reached",
                       "The question did not distinguish an archive that is the unit's own from one shared with "
                       "other units", "could reach a shared archive's members too",
                       "Leaving those members out was the implementation's rule",
                       "no decision of the user's stood behind it until the second round",
                       f'second-round answer 1, "{_SECOND_ROUND[1]}"', "left out of every unit, on record",
                       "with `applied` false, the reason `shared_archive`"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)
        for document in DOCUMENTS:
            text = " ".join((_ROOT / document).read_text(encoding="utf-8").split())
            with self.subTest(document=document):
                self.assertNotIn("The question put to the user concerned such a unit-scoped archive", text)
        decisions = _trial_decisions()
        user = [entry for entry in decisions
                if entry.get("at") == "2026-10-08" and _A_TO_D_REQUEST in str(entry["decision"])][0]
        self.assertIn(_QUESTION_2, str(user["decision"]))
        self.assertIn("The question did not distinguish an archive that is the unit's own from one shared with other "
                      "units", str(user["decision"]))
        agent = [entry for entry in decisions
                 if entry.get("by") == "agent" and "unattributed_member" in str(entry["decision"])][0]
        for phrase in ("for its unit-scoped half", "which no decision of the user's settled then",
                       "(second-round answer 1, the last entry of that date)", "second-round answer 3"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, str(agent["state"]))

    def test_the_shared_file_name_case_is_stated_as_decided_in_the_second_round(self) -> None:
        """Interactive #69 and gate #37 first keyed each input's energy set by file name alone; the user's second-round
        answer 2 had both key it by relative path before #69 merges. The skill and the trial manifest say so as well as
        the contract (whose passage _DECIDED_CASES holds), and no document still states the interim of the open
        point."""
        skill = " ".join((_ROOT / _BATCH_SKILL).read_text(encoding="utf-8").split())
        for phrase in ("two inputs of the unit share a file name in different folders",
                       "keyed each input's set by file name alone", "lost its raw data without a run",
                       f'second-round answer 2 of 2026-10-08 ("{_SECOND_ROUND[2]}")', "by its relative path",
                       "`path_relative_to_input_directory`", "before #69 merges",
                       "holds every unit whose sets differ, whatever the names"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)
        decisions = _trial_decisions()
        first = [entry for entry in decisions
                 if entry.get("at") == "2026-10-08" and _A_TO_D_REQUEST in str(entry["decision"])][0]
        for phrase in ("As first opened, both keyed each input's set by its file name alone",
                       "question 6 did not cover that case, and the second round of the same date decided it",
                       "both now key each input's set by its relative path"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, str(first["result"]))
        second = _second_round_entry()
        for phrase in ("aif_collision_energies_by_input_key path_relative_to_input_directory",
                       "FAILs a record keyed by basename for an input in a subfolder",
                       "gate #37 merges with or before the campaign's pin to Interactive 0.5.36"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, str(second["result"]))
        for document in DOCUMENTS:
            text = " ".join((_ROOT / document).read_text(encoding="utf-8").split())
            for words in _WITHDRAWN:
                with self.subTest(document=document, words=words):
                    self.assertNotIn(words, text)
        for entry in decisions:
            for words in _WITHDRAWN:
                with self.subTest(entry=str(entry["decision"])[:60], words=words):
                    self.assertNotIn(words, json.dumps(entry, ensure_ascii=False))

    def test_the_cases_the_agent_had_read_are_stated_as_decided_on_2026_10_08(self) -> None:
        """Answers 1 to 5 settle what merged code did or the agent had read or proposed: each agent entry says the
        user decided it, and the contract states each as the user's."""
        agent = [entry for entry in _trial_decisions() if entry.get("by") == "agent"]
        for word, number in (("PAIR-1, the check", 1), ("unattributed_member", 2),
                             ("aif_collision_energy_unrecorded", 3), ("acquisition_declared_only", 4),
                             ("disposition_held", 5)):
            with self.subTest(word=word):
                entries = [entry for entry in agent if word in str(entry["decision"])]
                self.assertEqual(1, len(entries))
                self.assertRegex(entries[0]["state"], r"(decided|accepted) by the user on 2026-10-08")
                self.assertIn(f"(answer {number}, the entry of that date)", entries[0]["state"])
        contract = " ".join(self.contract.split())
        for phrase in ("`acquisition_declared_only`", "the user accepted it on 2026-10-08",
                       "the user decided on 2026-10-08 that this hold governs",
                       "the user decided on 2026-10-08 that such a unit runs so",
                       "only on the user's explicit word", "boundary 5 does not cover it"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, contract)
        self.assertNotIn("no longer lets run", contract)
        self.assertNotIn("concern the same list", contract)

    def test_only_the_three_points_of_answer_3_are_marked_open_for_the_user(self) -> None:
        """The answers of 2026-10-08 settled every case the draft had left open, and the second round the points review
        found after them, but for three points of its answer 3 that review found after it: only the passage that states
        those three is marked open, and no agent entry since 2026-10-06 marks a passage open."""
        found = {}
        for document in DOCUMENTS:
            for block in _blocks((_ROOT / document).read_text(encoding="utf-8")):
                marks = [mark for mark in _OPEN_MARKS if mark in block]
                if marks:
                    found.setdefault(document, []).append(block[:len(_SECOND_ROUND_UNSETTLED)])
        self.assertEqual({"CLAUDE.md": [_SECOND_ROUND_UNSETTLED]}, found)
        for entry in _trial_decisions():
            if entry.get("by") == "agent" and str(entry.get("at", "")) >= "2026-10-06":
                state = str(entry.get("state", ""))
                with self.subTest(entry=str(entry["decision"])[:60]):
                    self.assertFalse([mark for mark in _OPEN_MARKS if mark in state])
        marked = self.contract.replace(_SECOND_ROUND_HEADING, _SECOND_ROUND_HEADING + " One point is open for the user.")
        self.assertTrue(any(mark in block for block in _blocks(marked) for mark in _OPEN_MARKS))

    def test_the_second_round_answers_are_the_users_and_quoted_as_the_question_showed_them(self) -> None:
        """The contract lists the four second-round answers in Evidence and decisions, each quoting the chosen option's
        label exactly as the question showed it (or the user's own words), and the trial manifest records them as the
        user's, with the pull requests and heads that implement them."""
        blocks = _blocks(_section(self.contract, "## Evidence and decisions"))
        heads = [index for index, block in enumerate(blocks) if block.startswith(_SECOND_ROUND_HEADING)]
        self.assertEqual(1, len(heads))
        for phrase in ("three points that no answer of 2026-10-08 covered", "gate #36 had left one choice to the user",
                       "the same question form", "answered all four on 2026-10-08",
                       "exactly as the question showed it, its letter included", "the agent's rendering, not a quote",
                       *(f"{name} (at `{head}`)" for name, head in (("Interactive #69", "4722776"),)),
                       "#37 (at `c716368`)", "#36 (at `f043ab1`)"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, blocks[heads[0]])
        items = blocks[heads[0] + 1:heads[0] + 1 + len(_SECOND_ROUND)]
        for number, words in _SECOND_ROUND.items():
            with self.subTest(answer=number):
                item = items[number - 1]
                self.assertTrue(item.startswith(f"- second-round answer {number}, "), item[:60])
                self.assertIn(f'"{words}"', item)
        self.assertIn("The agent reads the rest as the decision", items[3])
        self.assertIn("with no exception for one planned before the statement", items[3])
        self.assertIn("implement the answers, but for the three points of answer 3 after the list", blocks[heads[0]])
        self.assertTrue(blocks[heads[0] + 1 + len(_SECOND_ROUND)].startswith(_SECOND_ROUND_UNSETTLED))
        self.assertNotIn("leaves none of this amendment's rules", " ".join(self.contract.split()))
        second = _second_round_entry()
        self.assertTrue(str(second["by"]).startswith("user"))
        decision, result = str(second["decision"]), str(second["result"])
        for number, words in _SECOND_ROUND.items():
            with self.subTest(answer=number):
                self.assertRegex(decision, rf"Second-round answer {number}, [^.]*" + re.escape(words))
        self.assertIn("exactly as the question showed it, its letter included", decision)
        self.assertIn("the agent reads the rest as", decision)
        for name, head in _SECOND_ROUND_HEADS.items():
            with self.subTest(pull_request=name):
                self.assertIn(f"{name} (", result)
                self.assertIn(head, result)

    def test_the_three_points_of_answer_3_the_code_does_not_settle_are_left_to_the_user(self) -> None:
        """Answer 3 keeps the members a Catalog declaration does not name left out, on record, but Interactive #69
        records nothing of its own for them; where the encoding order cannot choose, or a sample row admits an encoding
        the order puts after an unpaired twin, #69 follows the agent's reading, which the answer did not cover, and
        where the order ties and a sample row admits one, #69 records chosen_by encoding_order although the admission
        chose; and where the encoding a sample row admits is an mzML RawDataHandler cannot decode, #69 takes a readable
        unpaired twin in its place as an unattributed input, outside the sample's Class, recorded only in
        taken_instead_of_undecodable. The documents state all three as the user's to settle, not as the answer
        implemented."""
        blocks = _blocks(_section(self.contract, "## Evidence and decisions"))
        start = [index for index, block in enumerate(blocks) if block.startswith(_SECOND_ROUND_UNSETTLED)]
        self.assertEqual(1, len(start))
        for phrase in ("the code does not meet or that the answer did not cover", "Each is open for the user",
                       "decides none of them"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, blocks[start[0]])
        record, order, undecodable = blocks[start[0] + 1:start[0] + 4]
        for block, lead in zip((record, order, undecodable), _SECOND_ROUND_UNSETTLED_ITEMS):
            self.assertTrue(block.startswith(lead), block[:80])
        for phrase in ("records nothing of its own for them", "the archive's member listing is their only record",
                       "The code does not meet this part of the answer as written", "is the user's to say",
                       "with no reason recorded for it"):
            with self.subTest(item="record", phrase=phrase):
                self.assertIn(phrase, record)
        order = " ".join(order.split())
        for phrase in ("takes neither and leaves both out (`two_encodings_of_one_name`)", "not analysed at all",
                       "`admitted_by_the_unit`", "The answer covers none of these cases",
                       "Where no sample row admits either", "Where a sample row admits one of them",
                       "`chosen_by` `encoding_order`: the record names the order as the basis although the order "
                       "tied and the sample row's admission chose", "gate #37 accepts that record",
                       "a misstated basis in #69's record, which Interactive has not corrected",
                       "the agent's reading in #69", "not the user's decision"):
            with self.subTest(item="order", phrase=phrase):
                self.assertIn(phrase, order)
        undecodable = " ".join(undecodable.split())
        for phrase in ("sets the admitted mzML aside", "`unsupported_mzml_encoding`",
                       "takes the unpaired twin in its place as an unattributed input",
                       "not in the sample's Class, so the sample's data leave its contrast group",
                       "Only `unattributed_members.taken_instead_of_undecodable` records the substitution",
                       "gate #37 does not read it", "Before #69 that sample had no input at all",
                       "Neither answer 3 nor the rule of 2026-09-30", "the agent's reading in #69",
                       "not the user's decision", "is the user's to say"):
            with self.subTest(item="undecodable", phrase=phrase):
                self.assertIn(phrase, undecodable)
        self.assertTrue(blocks[start[0] + 4].startswith("Beyond these three points, what remains is the user's approval"))
        section = " ".join(_section(self.contract, "## Evidence and decisions").split())
        for phrase in ("implement it where it covers the case. Where they go beyond it or fall short of it, the item "
                       "says so", "That much is the answer.", "The rest of this item is the agent's reading in #69",
                       "which the answer did not cover and the user has not decided",
                       "Where a sample row admits a readable encoding of that sample",
                       "where the order ranks the two equal (recorded with `chosen_by` `encoding_order`, although the "
                       "admission chose)", "sets the admitted one aside too",
                       "an unattributed input outside the sample's Class "
                       "(`unattributed_members.taken_instead_of_undecodable`)",
                       "Where the order cannot choose and no sample row admits either",
                       "no encoding is taken: both are still left out as `two_encodings_of_one_name`",
                       'That falls short of answer 3\'s "on record" as written, and the code has not closed the gap'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)
        for withdrawn in ("Interactive #69 and gate #37 implement the answer:",
                          "Where a sample row admits an encoding of that sample, that one stays the sample's input",
                          "decode competes with none (`undecodable_mzml_set_aside`)", "those two points are the user's"):
            with self.subTest(withdrawn=withdrawn):
                self.assertNotIn(withdrawn, section)
        result = str(_second_round_entry()["result"])
        for phrase in ("These cases are the agent's reading in Interactive #69",
                       "the answer did not cover and the user has not decided",
                       "the other is left out with chosen_by encoding_order, although the admission chose, a misstated "
                       "basis Interactive has not corrected",
                       "recorded only in unattributed_members.taken_instead_of_undecodable, which gate #37 does not read",
                       "That too is the agent's reading, not the user's decision",
                       'That falls short of the answer\'s "on record" as written', "is the user's to say",
                       "the code has not closed the gap"):
            with self.subTest(entry="second round", phrase=phrase):
                self.assertIn(phrase, result)
        self.assertNotIn("These two cases", result)
        agent = [entry for entry in _trial_decisions()
                 if entry.get("by") == "agent" and "unattributed_member" in str(entry["decision"])][0]
        for phrase in ("three points of that answer review found unmet or uncovered and left to the user",
                       "its twin recorded as chosen by the encoding order although the admission chose",
                       "(unattributed_members.taken_instead_of_undecodable), also the agent's reading"):
            with self.subTest(entry="agent", phrase=phrase):
                self.assertIn(phrase, agent["state"])

    def test_the_members_a_unit_scoped_archive_leaves_out_are_stated_as_the_second_round_decided_them(self) -> None:
        """Interactive #64 left out of a unit-scoped archive an mzXML that only converts, a member whose path names the
        opposite polarity and one name in two encodings, and took none where the Catalog declared the inputs. The
        second round's answer 3 converts the mzXML in a campaign and takes one of two encodings by the encoding order
        (Interactive #69, gate #37). For the members a declaration does not name, Interactive records nothing of its
        own, and the documents say so rather than that they are in unattributed_members.left_out."""
        section = " ".join(_section(self.contract, "## Evidence and decisions").split())
        for phrase in (*_UNIT_SCOPED_LEFT_OUT, "for the members the implementation takes",
                       "Interactive #64 did not take every member no row pairs with",
                       "It took none where the Catalog declared the unit's analysis inputs",
                       "No decision of the user's stood behind these exclusions either, until the user decided on "
                       f'2026-10-08 (second-round answer 3, "{_SECOND_ROUND[3]}")',
                       "the rule of 2026-09-30 converts mzXML-only data", "Outside a campaign nothing is converted",
                       "the existing encoding order (a vendor folder or container, then mzML, then mzXML) takes one",
                       "Where the order cannot choose", "both are still left out as `two_encodings_of_one_name`",
                       "is still left out, on record (`polarity_token_contradicts_ion_mode`)",
                       "Interactive records nothing of its own for the members no declaration names",
                       "such a unit's manifest carries no `unattributed_members`",
                       "nothing names them as left out or says why", "INP-1, a `blocks_run` check, FAILs a left-out "
                       "member that reaches a run"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)
        self.assertNotIn("That settles the unit-scoped half.", section)
        decisions = _trial_decisions()
        agent = [entry for entry in decisions
                 if entry.get("by") == "agent" and "unattributed_member" in str(entry["decision"])][0]
        for reason in _UNIT_SCOPED_LEFT_OUT[:5]:
            with self.subTest(reason=reason):
                self.assertIn(reason.strip("`"), str(agent["decision"]))
        self.assertIn("so were the members it leaves out of a unit-scoped archive", str(agent["state"]))
        result = str(_second_round_entry()["result"])
        for phrase in ("unattributed_members.converted", "chosen_other_encoding",
                       "two_encodings_of_one_name remains only where the order cannot choose",
                       "Interactive records nothing of its own for the members no declaration names",
                       "the manifest carries no unattributed_members"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, result)

    def test_gate_37_merges_before_interactive_69_and_69_waits_for_no_answer(self) -> None:
        """The second round's answer 2 is met, so #69 no longer waits for the user; the next plan pins what Interactive
        main carries, so gate #37 goes in first or with it."""
        merges = " ".join(_section(self.contract, "## Merges").split())
        for phrase in ("Gate #37 merges before, or with, Interactive #69",
                       "no campaign pins Interactive 0.5.36 before it is in",
                       "no longer waits for an answer of the user's", "second-round answer 2 of 2026-10-08"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, merges)
        self.assertNotIn("is not merged under this delegation", merges)

    def test_the_differing_energy_hold_is_not_stated_as_standing(self) -> None:
        """Answer 6 replaced the hold of Interactive 0.5.34 and gate #34; the documents say what replaces it, which
        pull requests implement it and in which order they go in."""
        for document in ("CLAUDE.md", _BATCH_SKILL):
            text = " ".join((_ROOT / document).read_text(encoding="utf-8").split())
            for phrase in ("`aif_energy_sets_differ_between_inputs`", "Interactive #69", "gate #37",
                           "`aif_collision_energies_differ_between_inputs`", 'option C, "そのまま解析する"'):
                with self.subTest(document=document, phrase=phrase):
                    self.assertIn(phrase, text)
            for earlier in ("only when every input records the same energies", "the same energies in every input"):
                with self.subTest(document=document, earlier=earlier):
                    self.assertNotIn(earlier, text)

    def test_the_rule_replaced_on_2026_10_08_is_marked_in_the_trial_manifest(self) -> None:
        """The warning-only rule of 2026-09-30 for an empty energy list was replaced by answer 3."""
        entries = [entry for entry in _trial_decisions()
                   if str(entry["decision"]).startswith("An AIF file whose collision-energy target list is empty")]
        self.assertEqual(1, len(entries))
        self.assertEqual("2026-09-30", entries[0]["at"])
        self.assertIn("replaced on 2026-10-08", str(entries[0]["decision"]))
        self.assertTrue(str(entries[0]["result"]).startswith("Replaced on 2026-10-08 by the user's answer 3"))
        self.assertNotIn("Stands as the user's rule", str(entries[0]["result"]))

    def test_the_first_pilot_is_revoked_and_its_re_run_scoped(self) -> None:
        section = " ".join(_section(self.contract, "## Confirmation boundaries").split())
        for phrase in ("it has been revoked", "the first pilot's 10 units that did not end done",
                       "`plan --replan-from`", "a new manifest with its own approval",
                       "The first pilot's profile, which turned RT correction off, is no longer approvable"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

    def test_a_policy_override_of_rt_correction_is_stated_and_covered_by_the_digest(self) -> None:
        """The review asked whether "always on" could be undone by a --policy override; gate #36 answers it. The
        second round's answer 4 refuses every manifest without the statement, with no exception for older ones."""
        section = " ".join(_section(self.contract, "## Evidence and decisions").split())
        for phrase in ("`plan --policy`", "it is not refused", "Gate #36", "the summary's second line",
                       "repeated on stderr", "`policy_overrides`", "the sha256 digest the person approves covers them",
                       "no longer matches its own policy and profile is not approvable",
                       "the correction is on by default (second-round answer 4, above)",
                       "every manifest that carries no automatic RT correction statement",
                       "with no exception for one planned before the statement", "tells the person to plan again",
                       "a manifest planned now with the default policy states the correction on"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)
        self.assertNotIn("legacy manifest", section)
        skill = " ".join((_ROOT / _BATCH_SKILL).read_text(encoding="utf-8").split())
        for phrase in ("automatic RT correction statement", "is refused and planned again",
                       "second-round answer 4 of 2026-10-08"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)

    def test_the_go_signal_is_the_users(self) -> None:
        words = "本番開始前には私がGoサインを出すので、聞いてください。"
        self.assertIn(words, self.contract)
        entries = [entry for entry in _trial_decisions() if words in str(entry["decision"])]
        self.assertEqual(1, len(entries))
        self.assertEqual("2026-10-01", entries[0]["at"])
        self.assertTrue(str(entries[0]["by"]).startswith("user"))
        # The skill runs from any directory, where this contract is not loaded, and the README states the approval.
        skill = " ".join((_ROOT / _BATCH_SKILL).read_text(encoding="utf-8").split())
        self.assertIn(words, skill)
        self.assertIn("never started or scheduled on the production campaign before the user's explicit go", skill)
        readme = " ".join((_ROOT / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("Nor is it the user's go", readme)

    def test_the_rules_replaced_on_2026_10_02_are_marked_in_the_trial_manifest(self) -> None:
        """The gate lists of 2026-10-01 and its mzXML rule with every inference off were replaced on 2026-10-02."""
        for opening in ("Before production, in the campaign: a FAIL stops a unit's MS-DIAL run only for",
                        "mzXML conversion, in the campaign, as relayed with the campaign rules on 2026-10-01"):
            with self.subTest(opening=opening):
                entries = [entry for entry in _trial_decisions() if str(entry["decision"]).startswith(opening)]
                self.assertEqual(1, len(entries))
                self.assertIn("replaced on 2026-10-02", str(entries[0]["decision"]))
                self.assertIn("Replaced on 2026-10-02", str(entries[0]["result"]))
                self.assertNotIn("awaiting the user's decision", str(entries[0]["result"]))

    def test_a_unit_flagged_by_its_studys_text_alone_is_not_stated_as_excluded(self) -> None:
        """Before 2026-10-03 the contract excluded ion-mobility data "whether the repository or the raw headers
        show it", and the plan read the repository's column, set from text many units share."""
        heading, _lead, _phrases = _DECIDED_CASES["ion mobility from unit-level evidence"]
        section = _section(self.contract, heading)
        earlier = section.replace("is not such evidence", "is evidence too")
        self.assertNotEqual(section, earlier)
        problems = _case_problems(self.contract.replace(section, earlier))
        self.assertTrue(any(item.startswith("ion mobility from unit-level evidence:") for item in problems), problems)
        self.assertNotIn("whether the repository or the raw headers show it", " ".join(section.split()))

    def test_every_description_of_the_discard_fallback_names_its_policy_field(self) -> None:
        found = {}
        for document in DOCUMENTS:
            text = (_ROOT / document).read_text(encoding="utf-8")
            if _fallback_without_its_field(text):
                found[document] = _fallback_without_its_field(text)
        self.assertEqual({}, found)
        # At 79a251c the fallback named no switch, so nothing told the runner whether the user allowed it.
        bare = self.contract.replace(f"`{_DISCARD_FALLBACK_FIELD}: true`", "an approval")
        self.assertEqual(2, len(_fallback_without_its_field(bare)))

    def test_the_lists_are_read_from_their_bullets_only(self) -> None:
        section = (
            "## Gate verdicts in a campaign\n\n"
            "- `blocks_run`: ELIG-1 and\n  ACQ-1. Such a unit fails.\n"
            "- `record_only`: CLS-1.\n\nSUM-1 is named here, in neither list.\n"
        )
        self.assertEqual({"ELIG-1", "ACQ-1"}, _gate_list(section, "blocks_run"))
        self.assertEqual({"CLS-1"}, _gate_list(section, "record_only"))
        decided = _decided_lists([
            {"decision": "Only ELIG-1 and ACQ-1 stop it (blocks_run). CLS-1 is recorded (record_only)."},
            {"decision": "Later: CNT-1 stops it too (blocks_run)."},
        ])
        self.assertEqual({"blocks_run": {"CNT-1"}, "record_only": {"CLS-1"}}, decided)
        text = ("a FAIL in a `blocks_run` check\n   (ELIG-1 and ACQ-1), and a `record_only` check (CLS-1); a "
                "`blocks_run` check (the user's decision) is no list.")
        self.assertEqual([{"ELIG-1", "ACQ-1"}], _parenthesised_lists(text, "blocks_run"))
        self.assertEqual([{"CLS-1"}], _parenthesised_lists(text, "record_only"))


class StopIsPerUnitTests(unittest.TestCase):
    """What the contract says a stop is, held to the runner the campaign runs (scripts/campaign)."""

    def setUp(self) -> None:
        self.contract = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        heading, lead, _phrases = _DECIDED_CASES["a stop is per unit"]
        self.passage = next(block for block in _blocks(_section(self.contract, heading)) if block.startswith(lead))

    def test_every_held_state_of_the_runner_is_named_in_the_gate_rule(self) -> None:
        sys.path.insert(0, str(_ROOT / "scripts"))
        try:
            from campaign import ledger
        finally:
            sys.path.remove(str(_ROOT / "scripts"))
        section = _section(self.contract, _GATE_RULE_SECTION)
        self.assertEqual(("gate_held", "contract_held"), ledger.HELD_STATES)
        for state in ledger.HELD_STATES:
            with self.subTest(state=state):
                self.assertIn(f"`{state}`", section)

    def test_the_runners_four_campaign_pauses_are_the_ones_named(self) -> None:
        """The pauses the contract names are the runner's own (policy.CAMPAIGN_PAUSES), each by its name."""
        sys.path.insert(0, str(_ROOT / "scripts"))
        try:
            from campaign import policy
        finally:
            sys.path.remove(str(_ROOT / "scripts"))
        self.assertEqual({"pin", "disk", "outage", "backend"}, set(policy.CAMPAIGN_PAUSES))
        for kind, name in policy.CAMPAIGN_PAUSES.items():
            with self.subTest(kind=kind):
                words = name.split(" ", 1)[1]  # "a pin change" is "A pin change, ..." in the passage
                self.assertIn(words, self.passage)

    def test_no_pause_waits_for_an_operator(self) -> None:
        """Before 2026-10-02 one unit's broken record paused the campaign until an operator resumed it."""
        for earlier in ("until an operator resumes", "only an operator lifts", "waits for an operator"):
            with self.subTest(earlier=earlier):
                self.assertNotIn(earlier, self.passage)
                put_back = self.contract.replace("Nothing one unit does pauses the campaign.",
                                                 f"A broken contract pauses the campaign {earlier}.")
                self.assertTrue(any(item.startswith("a stop is per unit:") for item in _case_problems(put_back)))


class TrialDecisionsTests(unittest.TestCase):
    """The trial manifest's decisions are appended as they are taken: each says when, and what."""

    def test_decisions_are_dated_and_in_order(self) -> None:
        decisions = json.loads(
            (_ROOT / "trials" / "2026-09-20-ten-unit-trial-manifest.json").read_text(encoding="utf-8")
        )["decisions"]
        for entry in decisions:
            with self.subTest(entry=entry.get("decision", "")[:60]):
                self.assertRegex(entry["at"], r"^\d{4}-\d{2}-\d{2}")
                self.assertTrue(str(entry["decision"]).strip())
        dates = [entry["at"][:10] for entry in decisions]
        self.assertEqual(sorted(dates), dates)


if __name__ == "__main__":
    unittest.main()
