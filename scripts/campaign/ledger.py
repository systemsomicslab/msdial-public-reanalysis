"""The campaign ledger: one SQLite file that holds every unit's state and how it got there.

WHERE. <workspace_root>\\_campaigns\\<campaign_id>\\ledger.sqlite, beside the approved manifest, the
authorization record, the copied handoffs and the gate reports. Nothing in it names a private library's
location: libraries are named by file name and sha256, and every argument recorded with an attempt has
passed through policy.redactor first.

WHY EVERY TRANSITION IS A TRANSACTION. A campaign runs for weeks, through reboots. The runner commits a
unit's new state, the transition row that says why and the attempt or Console record it closes in one
transaction, and does nothing between one commit and the next that it could not redo. A crash therefore
leaves the ledger at a committed transition, and the state machine resumes from it; tests/
test_campaign_resume.py stops the runner at every commit and checks exactly that.

WHAT THE SCHEMA ITSELF REFUSES. The CHECK constraints and triggers below are the rules that must hold
whatever code writes here:
- a transition that crosses a confirmation boundary names the approval that covers it, that approval
  covers the boundary, and it has not been revoked;
- transitions are append-only, and so is a closed attempt;
- one Console at a time: the console_slot row is a singleton and cannot be taken over while held;
- a terminal state carries its reason, and a waiting state what it resumes and when;
- one campaign per ledger, whose approval is of that campaign's manifest digest.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

# 2 (2026-10-02): the gate_held and contract_held states, the recheck_held request and the gate verdict's
# blocking unevaluated checks. A ledger of schema 1 is brought to 2 when it is opened (Ledger._migrate).
SCHEMA_VERSION = 2

ACTIVE_STATES = (
    "pending", "class_settled", "handoff_ready",
    "downloading", "downloaded", "preflighted",
    "splitting", "split_parent",
    "metadata_prepared", "diagnosing", "diagnosed", "prepared", "running", "run_done",
    "published", "gated", "releasing", "discarding",
)
# queued: a split part waiting its turn, so the parts of one split enter the pipeline one at a time.
# gate_held: a unit the before-production gate gave no usable report for (the user's rule of 2026-10-02). It
# has not run, keeps its raw data and is counted nothing, and waits for the gate to be run again: at the
# runner's next start, held_recheck_seconds after each try, or at an operator's recheck-held.
# contract_held: a unit Interactive gave a reply or a record for that the runner cannot read or act on (no
# campaign_disposition, a malformed one, one another extractor made, a reply of another shape). "Stop" is
# per unit (2026-10-02), so it is held as gate_held is, never the campaign paused for it, and the step it
# was held at is made again at the same rechecks.
WAITING_STATES = ("waiting_retry", "deferred_disk", "queued", "gate_held", "contract_held")
# The waiting states a recheck releases, by itself or at an operator's recheck-held.
HELD_STATES = ("gate_held", "contract_held")
TERMINAL_STATES = (
    "done", "skipped", "excluded", "failed", "split_done", "stopped_no_approval", "stopped_policy_drift",
)
STATES = ACTIVE_STATES + WAITING_STATES + TERMINAL_STATES
# Where a unit waiting on something can resume: any state with work of its own.
RESUMABLE_STATES = tuple(state for state in ACTIVE_STATES if state != "split_parent")
BOUNDARIES = ("1", "3", "4", "5", "split")
# held: raw data the rules delete and Interactive did not (a failed run that left an mzTab-M, a deletion a
# finalisation hold kept refusing). kept: raw data the rules keep (retention keep, no live boundary 5).
RAW_DISPOSITIONS = ("none", "present", "released", "discarded", "kept", "held", "deferred_to_parent")
ATTEMPT_OUTCOMES = (
    "ok", "failed", "timeout", "cancelled", "interrupted", "stalled", "refused", "busy", "blocked", "fault",
)
# contract: what a runner before 2026-10-02 paused for when Interactive broke the contract it reads for one
# unit. The runner now holds that unit (contract_held) and makes no such pause; one an earlier runner left in
# a ledger is lifted by an operator's resume. fault: a backend that does not answer, or a repository outage,
# looked at again by itself after fault_recheck_seconds.
PAUSE_KINDS = ("operator", "contract", "pin", "fault", "disk")
# A pause never gives way to a lesser one: a disk that runs short while the operator has paused the
# campaign must not lift the operator's pause by replacing it.
PAUSE_RANK = {kind: len(PAUSE_KINDS) - index for index, kind in enumerate(PAUSE_KINDS)}
# recheck_held: make the step a held unit (HELD_STATES) was held at again now: for gate_held, the gate.
REQUEST_ACTIONS = ("skip", "retry", "release_held", "recheck_held")


def _in(values: Iterable[str]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS campaign(
    campaign_id TEXT PRIMARY KEY CHECK(length(trim(campaign_id)) > 0),
    created_at TEXT NOT NULL,
    pool TEXT NOT NULL CHECK(pool IN ('declared', 'acquisition_unknown')),
    manifest_path TEXT NOT NULL,
    manifest_digest TEXT NOT NULL
        CHECK(manifest_digest GLOB 'sha256:[0-9a-f]*' AND length(manifest_digest) = 71),
    analysis_purpose TEXT NOT NULL CHECK(length(trim(analysis_purpose)) > 0),
    workspace_root TEXT NOT NULL CHECK(length(trim(workspace_root)) > 0),
    raw_retention_policy TEXT NOT NULL CHECK(raw_retention_policy IN ('keep', 'delete_after_validated_output')),
    catalog_database TEXT NOT NULL,
    authorization_path TEXT NOT NULL,
    authorization_sha256 TEXT NOT NULL CHECK(length(authorization_sha256) = 64),
    policy_json TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    pins_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS campaign_is_one BEFORE INSERT ON campaign
WHEN (SELECT COUNT(*) FROM campaign) > 0
BEGIN SELECT RAISE(ABORT, 'one campaign per ledger'); END;
CREATE TRIGGER IF NOT EXISTS campaign_is_fixed BEFORE UPDATE ON campaign
BEGIN SELECT RAISE(ABORT, 'the approved campaign is fixed'); END;

CREATE TABLE IF NOT EXISTS approval(
    approval_id TEXT PRIMARY KEY CHECK(length(trim(approval_id)) > 0),
    campaign_id TEXT NOT NULL REFERENCES campaign(campaign_id),
    manifest_digest TEXT NOT NULL,
    approved_by TEXT NOT NULL CHECK(length(trim(approved_by)) > 0),
    approved_at TEXT NOT NULL CHECK(length(trim(approved_at)) > 0),
    statement TEXT NOT NULL CHECK(length(trim(statement)) > 0),
    covers_json TEXT NOT NULL CHECK(json_valid(covers_json) AND json_type(covers_json) = 'array'),
    recorded_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT,
    revoke_reason TEXT,
    CHECK((revoked_at IS NULL) = (revoked_by IS NULL)),
    CHECK(revoked_at IS NULL OR length(trim(coalesce(revoke_reason, ''))) > 0)
);
CREATE TRIGGER IF NOT EXISTS approval_of_this_manifest BEFORE INSERT ON approval
WHEN NEW.manifest_digest IS NOT (SELECT manifest_digest FROM campaign WHERE campaign_id = NEW.campaign_id)
BEGIN SELECT RAISE(ABORT, 'an approval is of the campaign manifest digest'); END;
CREATE TRIGGER IF NOT EXISTS approval_never_covers_2_or_6 BEFORE INSERT ON approval
WHEN EXISTS (SELECT 1 FROM json_each(NEW.covers_json) WHERE CAST(value AS TEXT) IN ('2', '6'))
BEGIN SELECT RAISE(ABORT, 'boundaries 2 and 6 are never covered by a campaign approval'); END;
CREATE TRIGGER IF NOT EXISTS approval_only_revoked BEFORE UPDATE ON approval
WHEN OLD.revoked_at IS NOT NULL
  OR NEW.approval_id IS NOT OLD.approval_id OR NEW.manifest_digest IS NOT OLD.manifest_digest
  OR NEW.covers_json IS NOT OLD.covers_json OR NEW.statement IS NOT OLD.statement
  OR NEW.approved_by IS NOT OLD.approved_by OR NEW.approved_at IS NOT OLD.approved_at
BEGIN SELECT RAISE(ABORT, 'an approval can only be revoked, once'); END;
CREATE TRIGGER IF NOT EXISTS approval_kept BEFORE DELETE ON approval
BEGIN SELECT RAISE(ABORT, 'approvals are never deleted'); END;

CREATE TABLE IF NOT EXISTS download_group(
    group_id TEXT PRIMARY KEY,
    unit_count INTEGER NOT NULL CHECK(unit_count >= 1),
    known_bytes INTEGER NOT NULL CHECK(known_bytes >= 0),
    size_known INTEGER NOT NULL CHECK(size_known IN (0, 1))
);

CREATE TABLE IF NOT EXISTS unit(
    unit_key TEXT PRIMARY KEY CHECK(length(trim(unit_key)) > 0),
    catalog_unit_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    accession TEXT NOT NULL,
    order_index INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'run' CHECK(role IN ('run', 'split_parent', 'split_part')),
    parent_unit_key TEXT REFERENCES unit(unit_key),
    download_group_id TEXT REFERENCES download_group(group_id),
    approved_class_digest TEXT,
    class_kind TEXT CHECK(class_kind IS NULL OR class_kind IN ('proposal', 'abstention')),
    known_bytes INTEGER NOT NULL DEFAULT 0 CHECK(known_bytes >= 0),
    size_known INTEGER NOT NULL DEFAULT 0 CHECK(size_known IN (0, 1)),
    has_archive INTEGER NOT NULL DEFAULT 0 CHECK(has_archive IN (0, 1)),
    instrument TEXT NOT NULL DEFAULT '',
    ion_mode TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK(state IN {_in(STATES)}),
    state_since TEXT NOT NULL,
    resume_state TEXT CHECK(resume_state IS NULL OR resume_state IN {_in(RESUMABLE_STATES)}),
    pending_terminal TEXT CHECK(pending_terminal IS NULL OR pending_terminal IN ('skipped', 'excluded', 'failed')),
    failures INTEGER NOT NULL DEFAULT 0 CHECK(failures >= 0),
    interruptions INTEGER NOT NULL DEFAULT 0 CHECK(interruptions >= 0),
    next_attempt_at TEXT,
    terminal_reason TEXT,
    terminal_detail TEXT,
    workspace TEXT,
    manifest_path TEXT,
    handoff_path TEXT,
    authorization_copy TEXT,
    class_proposal_id TEXT,
    download_job_id TEXT,
    diagnostic_job_id TEXT,
    run_job_id TEXT,
    input_path TEXT,
    threshold_step INTEGER CHECK(threshold_step IS NULL OR threshold_step IN (100, 1000)),
    minimum_peak_height REAL CHECK(minimum_peak_height IS NULL OR minimum_peak_height >= 0),
    diagnostic_peak_count INTEGER CHECK(diagnostic_peak_count IS NULL OR diagnostic_peak_count >= 0),
    representative_file TEXT,
    order_source TEXT,
    disposition_json TEXT CHECK(disposition_json IS NULL OR json_valid(disposition_json)),
    outputs_produced INTEGER NOT NULL DEFAULT 0 CHECK(outputs_produced IN (0, 1)),
    gate_exit_pre INTEGER CHECK(gate_exit_pre IS NULL OR gate_exit_pre IN (0, 2, 3, 4)),
    gate_exit_final INTEGER CHECK(gate_exit_final IS NULL OR gate_exit_final IN (0, 2, 3, 4)),
    raw_disposition TEXT NOT NULL DEFAULT 'none' CHECK(raw_disposition IN {_in(RAW_DISPOSITIONS)}),
    raw_detail TEXT,
    downloaded_bytes INTEGER CHECK(downloaded_bytes IS NULL OR downloaded_bytes >= 0),
    peak_workspace_bytes INTEGER CHECK(peak_workspace_bytes IS NULL OR peak_workspace_bytes >= 0),
    updated_at TEXT NOT NULL,
    CHECK((role = 'split_part') = (parent_unit_key IS NOT NULL)),
    CHECK((state IN {_in(TERMINAL_STATES)}) = (terminal_reason IS NOT NULL)),
    CHECK(state NOT IN {_in(WAITING_STATES)} OR resume_state IS NOT NULL),
    CHECK(state <> 'waiting_retry' OR next_attempt_at IS NOT NULL),
    CHECK(state <> 'discarding' OR pending_terminal IS NOT NULL),
    CHECK(outputs_produced = 0 OR raw_disposition <> 'discarded')
);
CREATE INDEX IF NOT EXISTS unit_schedule ON unit(state, order_index, unit_key);

CREATE TABLE IF NOT EXISTS transition(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_key TEXT NOT NULL REFERENCES unit(unit_key),
    at TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL CHECK(to_state IN {_in(STATES)}),
    boundary TEXT CHECK(boundary IS NULL OR boundary IN {_in(BOUNDARIES)}),
    approval_id TEXT REFERENCES approval(approval_id),
    detail_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    CHECK(boundary IS NULL OR approval_id IS NOT NULL)
);
CREATE TRIGGER IF NOT EXISTS transition_append_only_update BEFORE UPDATE ON transition
BEGIN SELECT RAISE(ABORT, 'transitions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS transition_append_only_delete BEFORE DELETE ON transition
BEGIN SELECT RAISE(ABORT, 'transitions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS transition_boundary_live BEFORE INSERT ON transition
WHEN NEW.boundary IS NOT NULL AND (
    (SELECT revoked_at FROM approval WHERE approval_id = NEW.approval_id) IS NOT NULL
    OR NOT EXISTS (SELECT 1 FROM approval WHERE approval_id = NEW.approval_id)
)
BEGIN SELECT RAISE(ABORT, 'a boundary is crossed only under a live approval'); END;
CREATE TRIGGER IF NOT EXISTS transition_boundary_covered BEFORE INSERT ON transition
WHEN NEW.boundary IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM approval, json_each(approval.covers_json)
    WHERE approval.approval_id = NEW.approval_id AND CAST(json_each.value AS TEXT) = NEW.boundary
)
BEGIN SELECT RAISE(ABORT, 'the approval does not cover this boundary'); END;

CREATE TABLE IF NOT EXISTS attempt(
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_key TEXT NOT NULL REFERENCES unit(unit_key),
    step TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT CHECK(outcome IS NULL OR outcome IN {_in(ATTEMPT_OUTCOMES)}),
    counted INTEGER NOT NULL DEFAULT 0 CHECK(counted IN (0, 1)),
    tool TEXT,
    arguments_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(arguments_json)),
    job_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    CHECK((ended_at IS NULL) = (outcome IS NULL)),
    CHECK(counted = 0 OR outcome NOT IN ('ok', 'busy', 'fault'))
);
CREATE TRIGGER IF NOT EXISTS attempt_closed_is_final BEFORE UPDATE ON attempt
WHEN OLD.ended_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'a closed attempt is final'); END;
CREATE TRIGGER IF NOT EXISTS attempt_kept BEFORE DELETE ON attempt
BEGIN SELECT RAISE(ABORT, 'attempts are never deleted'); END;

CREATE TABLE IF NOT EXISTS console_run(
    console_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_key TEXT NOT NULL REFERENCES unit(unit_key),
    kind TEXT NOT NULL CHECK(kind IN ('diagnostic', 'production')),
    requested_at TEXT NOT NULL,
    job_id TEXT,
    console_sha256 TEXT NOT NULL CHECK(length(console_sha256) = 64),
    timeout_s REAL NOT NULL CHECK(timeout_s >= 0),
    idle_timeout_s REAL NOT NULL CHECK(idle_timeout_s >= 0),
    ended_at TEXT,
    outcome TEXT,
    CHECK((ended_at IS NULL) = (outcome IS NULL))
);
CREATE TABLE IF NOT EXISTS console_slot(
    id INTEGER PRIMARY KEY CHECK(id = 1),
    console_run_id INTEGER REFERENCES console_run(console_run_id),
    held_since TEXT,
    CHECK((console_run_id IS NULL) = (held_since IS NULL))
);
INSERT OR IGNORE INTO console_slot(id) VALUES (1);
CREATE TRIGGER IF NOT EXISTS console_slot_one_at_a_time BEFORE UPDATE ON console_slot
WHEN OLD.console_run_id IS NOT NULL AND NEW.console_run_id IS NOT NULL
     AND NEW.console_run_id IS NOT OLD.console_run_id
BEGIN SELECT RAISE(ABORT, 'one Console at a time'); END;
CREATE TRIGGER IF NOT EXISTS console_slot_stays BEFORE DELETE ON console_slot
BEGIN SELECT RAISE(ABORT, 'the Console slot is a singleton'); END;

CREATE TABLE IF NOT EXISTS gate_verdict(
    verdict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_key TEXT NOT NULL REFERENCES unit(unit_key),
    at TEXT NOT NULL,
    point TEXT NOT NULL CHECK(point IN ('before_production', 'pre_cleanup', 'final')),
    stage_arg TEXT NOT NULL,
    strict INTEGER NOT NULL CHECK(strict IN (0, 1)),
    outcome TEXT NOT NULL CHECK(outcome IN ('ran', 'timeout', 'error')),
    exit_code INTEGER CHECK(exit_code IS NULL OR exit_code IN (0, 2, 3, 4)),
    stage_reached TEXT,
    fail_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(fail_ids_json)),
    warn_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(warn_ids_json)),
    strict_hold_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(strict_hold_ids_json)),
    -- The FAILs that stop a unit's run (blocks_run): 'gate' when its run_policy was read with the runner's list.
    blocking_fail_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(blocking_fail_ids_json)),
    run_policy_source TEXT,
    report_path TEXT,
    report_sha256 TEXT,
    gate_commit TEXT NOT NULL,
    detail TEXT,
    -- The blocks_run checks left not evaluable where they are required, which stop the run as a FAIL does
    -- (2026-10-02). Last, where schema 1's ledger gains it (_migrate).
    blocking_unevaluated_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(blocking_unevaluated_ids_json)),
    CHECK(outcome <> 'ran' OR exit_code IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS disk_event(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    volume TEXT NOT NULL,
    free_bytes INTEGER,
    total_bytes INTEGER,
    reserve_bytes INTEGER,
    needed_bytes INTEGER,
    unit_key TEXT,
    action TEXT NOT NULL CHECK(action IN ('admit', 'pause', 'resume', 'cancel_download', 'never_fits'))
);

CREATE TABLE IF NOT EXISTS runner(
    id INTEGER PRIMARY KEY CHECK(id = 1),
    pid INTEGER,
    process_created_at REAL,
    host TEXT,
    started_at TEXT,
    heartbeat_at TEXT,
    paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0, 1)),
    pause_kind TEXT CHECK(pause_kind IS NULL OR pause_kind IN {_in(PAUSE_KINDS)}),
    pause_reason TEXT,
    paused_at TEXT,
    CHECK((paused = 1) = (pause_kind IS NOT NULL)),
    CHECK(paused = 0 OR length(trim(coalesce(pause_reason, ''))) > 0)
);
INSERT OR IGNORE INTO runner(id) VALUES (1);

CREATE TABLE IF NOT EXISTS campaign_event(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    unit_key TEXT,
    detail_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json))
);

CREATE TABLE IF NOT EXISTS request(
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN {_in(REQUEST_ACTIONS)}),
    unit_key TEXT NOT NULL REFERENCES unit(unit_key),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    requested_by TEXT NOT NULL DEFAULT '',
    handled_at TEXT,
    handled_detail TEXT
);
"""

UNIT_COLUMNS = frozenset({
    "catalog_unit_id", "repository", "accession", "order_index", "role", "parent_unit_key",
    "download_group_id", "approved_class_digest", "class_kind", "known_bytes", "size_known", "has_archive",
    "instrument", "ion_mode", "resume_state", "pending_terminal", "failures", "interruptions",
    "next_attempt_at", "terminal_reason", "terminal_detail", "workspace", "manifest_path", "handoff_path",
    "authorization_copy", "class_proposal_id", "download_job_id", "diagnostic_job_id", "run_job_id",
    "input_path", "threshold_step", "minimum_peak_height", "diagnostic_peak_count", "representative_file",
    "order_source", "disposition_json", "outputs_produced", "gate_exit_pre", "gate_exit_final",
    "raw_disposition", "raw_detail", "downloaded_bytes", "peak_workspace_bytes",
})


class LedgerError(RuntimeError):
    """A write the ledger refused: a stale transition, an unknown column, a missing approval."""


class ApprovalMissing(LedgerError):
    """A boundary was to be crossed with no live approval covering it."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _statements(script: str) -> list[str]:
    """A script's statements, each whole: a trigger's body holds semicolons of its own."""
    statements, current = [], ""
    for line in script.splitlines(keepends=True):
        current += line
        if sqlite3.complete_statement(current):
            if current.strip():
                statements.append(current.strip())
            current = ""
    return statements


class Ledger:
    """One campaign's ledger. Not shared between threads: the heartbeat opens its own connection.

    `commit_hook(phase)` is called with "before" just before each COMMIT and with "after" just after it.
    Only the resume tests set it: raising from "before" loses that transaction, as a crash before the
    commit would; raising from "after" stops the runner with it durable.
    """

    def __init__(
        self, path: str | Path, *, commit_hook: Callable[[str], None] | None = None, durable: bool = True
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), isolation_level=None, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        # FULL: a transition the runner acted on survives a power cut. Only tests pass durable=False, where
        # a crash is the commit hook raising and no disk is involved.
        self.connection.execute(f"PRAGMA synchronous = {'FULL' if durable else 'OFF'}")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.commit_hook = commit_hook
        self.commits = 0
        try:
            self._initialise()
        except BaseException:
            # A ledger that cannot be opened (a schema this code does not read, a migration that stopped)
            # keeps no handle on its file.
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _initialise(self) -> None:
        self.connection.executescript(SCHEMA)
        row = self.connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            self.connection.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
        elif int(row["value"]) == 1:
            self._migrate()
        elif int(row["value"]) != SCHEMA_VERSION:
            raise LedgerError(f"Ledger schema {row['value']} is not {SCHEMA_VERSION}.")

    # The tables whose CHECK constraints name a list schema 2 widened: unit and transition the states
    # (gate_held, contract_held), request the actions (recheck_held).
    _REBUILT_FOR_2 = ("unit", "transition", "request")

    def _migrate(self) -> None:
        """Bring a schema 1 ledger to schema 2, in one transaction, keeping every row it holds.

        SQLite cannot widen a CHECK constraint in place, so each table whose CHECK names a widened list is
        made anew from SCHEMA, filled from the old one, and put in its place: the procedure SQLite documents
        for a change ALTER TABLE cannot make, with foreign keys off while it runs and checked before it
        commits. Dropping a table drops its triggers and indexes (the append-only and approval triggers of
        transition among them), so every trigger and index of SCHEMA is made again in the same transaction.
        The gate verdict's new column is added in place. A crash leaves schema 1 as it was."""
        statements = _statements(SCHEMA)
        db = self.connection
        db.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.transaction():
                for table in self._REBUILT_FOR_2:
                    created = next(item for item in statements if item.startswith(f"CREATE TABLE IF NOT EXISTS {table}("))
                    db.execute(created.replace(f"CREATE TABLE IF NOT EXISTS {table}(", f"CREATE TABLE {table}_2(", 1))
                    db.execute(f"INSERT INTO {table}_2 SELECT * FROM {table}")
                    db.execute(f"DROP TABLE {table}")
                    db.execute(f"ALTER TABLE {table}_2 RENAME TO {table}")
                db.execute(
                    "ALTER TABLE gate_verdict ADD COLUMN blocking_unevaluated_ids_json TEXT NOT NULL DEFAULT '[]' "
                    "CHECK(json_valid(blocking_unevaluated_ids_json))"
                )
                for statement in statements:
                    if statement.startswith(("CREATE TRIGGER", "CREATE INDEX")):
                        db.execute(statement)
                broken = db.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise LedgerError(f"The schema 1 ledger's references do not resolve: {[tuple(row) for row in broken][:5]}.")
                db.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))
        finally:
            db.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            if self.commit_hook is not None:
                self.commit_hook("before")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        self.commits += 1
        if self.commit_hook is not None:
            self.commit_hook("after")

    # ---- the campaign --------------------------------------------------------------------------

    def create(
        self,
        campaign: dict[str, Any],
        approval: dict[str, Any],
        units: list[dict[str, Any]],
        groups: list[dict[str, Any]],
        now: str,
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO campaign(campaign_id, created_at, pool, manifest_path, manifest_digest, "
                "analysis_purpose, workspace_root, raw_retention_policy, catalog_database, authorization_path, "
                "authorization_sha256, policy_json, profile_json, pins_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    campaign["campaign_id"], now, campaign["pool"], campaign["manifest_path"],
                    campaign["manifest_digest"], campaign["analysis_purpose"], campaign["workspace_root"],
                    campaign["raw_retention_policy"], campaign["catalog_database"],
                    campaign["authorization_path"], campaign["authorization_sha256"],
                    _json(campaign["policy"]), _json(campaign["profile"]), _json(campaign["pins"]),
                ),
            )
            db.execute(
                "INSERT INTO approval(approval_id, campaign_id, manifest_digest, approved_by, approved_at, "
                "statement, covers_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval["approval_id"], campaign["campaign_id"], approval["manifest_digest"],
                    approval["approved_by"], approval["approved_at"], approval["statement"],
                    _json([str(item) for item in approval["covers"]]), now,
                ),
            )
            for group in groups:
                db.execute(
                    "INSERT INTO download_group(group_id, unit_count, known_bytes, size_known) VALUES (?, ?, ?, ?)",
                    (group["group_id"], group["unit_count"], group["known_bytes"], int(bool(group["size_known"]))),
                )
            for unit in units:
                columns = ["unit_key", "state", "state_since", "updated_at", *sorted(unit.keys() - {"unit_key"})]
                self._check_columns(set(columns) - {"unit_key", "state", "state_since", "updated_at"})
                db.execute(
                    f"INSERT INTO unit({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    [unit["unit_key"], "pending", now, now, *(unit[key] for key in columns[4:])],
                )
                db.execute(
                    "INSERT INTO transition(unit_key, at, from_state, to_state, detail_json) VALUES (?, ?, NULL, 'pending', ?)",
                    (unit["unit_key"], now, _json({"planned": True})),
                )

    def campaign(self) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM campaign").fetchone()
        if row is None:
            raise LedgerError("This ledger holds no campaign; run `approve` first.")
        result = dict(row)
        for key in ("policy", "profile", "pins"):
            result[key] = json.loads(result.pop(f"{key}_json"))
        return result

    def approval(self) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM approval ORDER BY recorded_at LIMIT 1").fetchone()
        if row is None:
            raise LedgerError("This ledger holds no approval.")
        result = dict(row)
        result["covers"] = json.loads(result.pop("covers_json"))
        return result

    def live_approval(self, boundary: str) -> dict[str, Any] | None:
        """The unrevoked approval covering `boundary`, or None."""
        approval = self.approval()
        if approval["revoked_at"] or str(boundary) not in {str(item) for item in approval["covers"]}:
            return None
        return approval

    def revoke(self, approval_id: str, by: str, reason: str, now: str) -> None:
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE approval SET revoked_at = ?, revoked_by = ?, revoke_reason = ? "
                "WHERE approval_id = ? AND revoked_at IS NULL",
                (now, by, reason, approval_id),
            ).rowcount
            if not changed:
                raise LedgerError(f"No live approval {approval_id} to revoke.")
            db.execute(
                "INSERT INTO campaign_event(at, kind, detail_json) VALUES (?, 'approval_revoked', ?)",
                (now, _json({"approval_id": approval_id, "by": by, "reason": reason})),
            )

    # ---- units ---------------------------------------------------------------------------------

    @staticmethod
    def _check_columns(columns: Iterable[str]) -> None:
        unknown = sorted(set(columns) - UNIT_COLUMNS)
        if unknown:
            raise LedgerError(f"Unknown unit columns: {', '.join(unknown)}.")

    def unit(self, unit_key: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM unit WHERE unit_key = ?", (unit_key,)).fetchone()
        if row is None:
            raise LedgerError(f"Unknown unit {unit_key}.")
        return dict(row)

    def units(self, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if states is None:
            rows = self.connection.execute("SELECT * FROM unit ORDER BY order_index, unit_key")
        else:
            wanted = list(states)
            rows = self.connection.execute(
                f"SELECT * FROM unit WHERE state IN ({', '.join('?' for _ in wanted)}) ORDER BY order_index, unit_key",
                wanted,
            )
        return [dict(row) for row in rows]

    def transition(
        self,
        unit_key: str,
        to_state: str,
        now: str,
        *,
        expect_from: str | Iterable[str] | None = None,
        boundary: str | None = None,
        detail: dict[str, Any] | None = None,
        close_attempt: tuple[int, str, bool, dict[str, Any] | None] | None = None,
        new_attempt: dict[str, Any] | None = None,
        console_job: tuple[int, str] | None = None,
        end_console_run: tuple[int, str] | None = None,
        gate: tuple[str, dict[str, Any]] | None = None,
        add_units: list[dict[str, Any]] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Move a unit to `to_state`, in one transaction with everything that records why.

        `expect_from` guards against a stale caller: the transition is refused unless the unit is in
        that state (or one of those states) when the transaction starts. `boundary` names the
        confirmation boundary crossed, and the live approval covering it is recorded with the row;
        without one ApprovalMissing is raised and nothing is written. In the same transaction:
        `close_attempt` closes an open attempt; `new_attempt` records a closed one (a job that ended
        while nothing in this process had it open); `console_job` names the job a Console run became;
        `end_console_run` ends a Console run and frees the slot; `gate` stores a gate verdict at its
        point; and `add_units` inserts new units (a split's parts).
        """
        self._check_columns(fields)
        approval_id = None
        if boundary is not None:
            approval = self.live_approval(boundary)
            if approval is None:
                raise ApprovalMissing(f"No live approval covers boundary {boundary}.")
            approval_id = approval["approval_id"]
        with self.transaction() as db:
            row = db.execute("SELECT state FROM unit WHERE unit_key = ?", (unit_key,)).fetchone()
            if row is None:
                raise LedgerError(f"Unknown unit {unit_key}.")
            current = row["state"]
            if expect_from is not None:
                allowed = {expect_from} if isinstance(expect_from, str) else set(expect_from)
                if current not in allowed:
                    raise LedgerError(
                        f"Unit {unit_key} is {current}, not {sorted(allowed)}; the transition to {to_state} "
                        "was refused."
                    )
            values = dict(fields)
            if to_state not in TERMINAL_STATES and "terminal_reason" not in values:
                values.setdefault("terminal_reason", None)
                values.setdefault("terminal_detail", None)
            if to_state not in WAITING_STATES:
                values.setdefault("resume_state", None)
                values.setdefault("next_attempt_at", None)
            if to_state != "discarding" and to_state not in TERMINAL_STATES:
                values.setdefault("pending_terminal", None)
            assignments = ", ".join(f"{key} = ?" for key in values)
            db.execute(
                f"UPDATE unit SET state = ?, state_since = ?, updated_at = ?"
                + (f", {assignments}" if assignments else "")
                + " WHERE unit_key = ?",
                [to_state, now, now, *values.values(), unit_key],
            )
            db.execute(
                "INSERT INTO transition(unit_key, at, from_state, to_state, boundary, approval_id, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (unit_key, now, current, to_state, boundary, approval_id, _json(detail or {})),
            )
            if close_attempt is not None:
                self._close_attempt(db, *close_attempt, now=now)
            if new_attempt is not None:
                self._insert_closed_attempt(db, unit_key, new_attempt, now=now)
            if console_job is not None:
                db.execute(
                    "UPDATE console_run SET job_id = ? WHERE console_run_id = ?", (console_job[1], console_job[0])
                )
            if end_console_run is not None:
                self._end_console_run(db, *end_console_run, now=now)
            if gate is not None:
                self._insert_gate(db, unit_key, gate[0], gate[1], now=now)
            for unit in add_units or []:
                columns = ["unit_key", "state", "state_since", "updated_at", *sorted(unit.keys() - {"unit_key", "state"})]
                self._check_columns(set(columns) - {"unit_key", "state", "state_since", "updated_at"})
                state = unit.get("state", "pending")
                db.execute(
                    f"INSERT OR IGNORE INTO unit({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    [unit["unit_key"], state, now, now, *(unit[key] for key in columns[4:])],
                )
                db.execute(
                    "INSERT INTO transition(unit_key, at, from_state, to_state, detail_json) VALUES (?, ?, NULL, ?, ?)",
                    (unit["unit_key"], now, state, _json({"split_from": unit_key})),
                )
        return self.unit(unit_key)

    def update(self, unit_key: str, now: str, **fields: Any) -> dict[str, Any]:
        """Change a unit's fields without moving it, such as the job id a start call returned."""
        self._check_columns(fields)
        if not fields:
            return self.unit(unit_key)
        with self.transaction() as db:
            db.execute(
                f"UPDATE unit SET {', '.join(f'{key} = ?' for key in fields)}, updated_at = ? WHERE unit_key = ?",
                [*fields.values(), now, unit_key],
            )
        return self.unit(unit_key)

    def transitions(self, unit_key: str | None = None) -> list[dict[str, Any]]:
        if unit_key is None:
            rows = self.connection.execute("SELECT * FROM transition ORDER BY seq")
        else:
            rows = self.connection.execute("SELECT * FROM transition WHERE unit_key = ? ORDER BY seq", (unit_key,))
        return [dict(row) for row in rows]

    # ---- attempts and Console runs -------------------------------------------------------------

    def open_attempt(self, unit_key: str, step: str, now: str, *, tool: str = "", arguments: Any = None) -> int:
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT INTO attempt(unit_key, step, started_at, tool, arguments_json) VALUES (?, ?, ?, ?, ?)",
                (unit_key, step, now, tool, _json(arguments or {})),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _close_attempt(
        db: sqlite3.Connection, attempt_id: int, outcome: str, counted: bool, detail: dict[str, Any] | None, *, now: str
    ) -> None:
        db.execute(
            "UPDATE attempt SET ended_at = ?, outcome = ?, counted = ?, detail_json = ?, "
            "job_id = coalesce(?, job_id) WHERE attempt_id = ? AND ended_at IS NULL",
            (now, outcome, int(bool(counted)), _json(detail or {}), (detail or {}).get("job_id"), attempt_id),
        )

    @staticmethod
    def _insert_closed_attempt(db: sqlite3.Connection, unit_key: str, attempt: dict[str, Any], *, now: str) -> None:
        detail = attempt.get("detail") or {}
        db.execute(
            "INSERT INTO attempt(unit_key, step, started_at, ended_at, outcome, counted, tool, arguments_json, "
            "job_id, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                unit_key, attempt["step"], attempt.get("started_at") or now, now, attempt["outcome"],
                int(bool(attempt.get("counted"))), attempt.get("tool") or "", _json(attempt.get("arguments") or {}),
                attempt.get("job_id") or detail.get("job_id"), _json(detail),
            ),
        )

    def recent_attempts(self, steps: Iterable[str], limit: int = 200) -> list[dict[str, Any]]:
        """The newest closed attempts of these steps across every unit, newest first."""
        wanted = list(steps)
        rows = self.connection.execute(
            f"SELECT * FROM attempt WHERE ended_at IS NOT NULL AND step IN ({', '.join('?' for _ in wanted)}) "
            "ORDER BY attempt_id DESC LIMIT ?",
            [*wanted, int(limit)],
        )
        return [dict(row) for row in rows]

    def count_attempts(self, unit_key: str, step: str, outcomes: Iterable[str]) -> int:
        wanted = list(outcomes)
        row = self.connection.execute(
            f"SELECT COUNT(*) FROM attempt WHERE unit_key = ? AND step = ? AND outcome IN ({', '.join('?' for _ in wanted)})",
            [unit_key, step, *wanted],
        ).fetchone()
        return int(row[0])

    def close_attempt(
        self, attempt_id: int, outcome: str, now: str, *, counted: bool = False, detail: dict[str, Any] | None = None
    ) -> None:
        with self.transaction() as db:
            self._close_attempt(db, attempt_id, outcome, counted, detail, now=now)

    def open_attempts(self, unit_key: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM attempt WHERE unit_key = ? AND ended_at IS NULL ORDER BY attempt_id", (unit_key,)
            )
        ]

    def attempts(self, unit_key: str | None = None) -> list[dict[str, Any]]:
        if unit_key is None:
            rows = self.connection.execute("SELECT * FROM attempt ORDER BY attempt_id")
        else:
            rows = self.connection.execute("SELECT * FROM attempt WHERE unit_key = ? ORDER BY attempt_id", (unit_key,))
        return [dict(row) for row in rows]

    def open_console_run(
        self, unit_key: str, kind: str, console_sha256: str, timeout_s: float, idle_timeout_s: float, now: str
    ) -> int:
        """Record a Console run before its start call, and take the one Console slot for it."""
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT INTO console_run(unit_key, kind, requested_at, console_sha256, timeout_s, idle_timeout_s) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (unit_key, kind, now, console_sha256, float(timeout_s), float(idle_timeout_s)),
            )
            run_id = int(cursor.lastrowid)
            db.execute(
                "UPDATE console_slot SET console_run_id = ?, held_since = ? WHERE id = 1", (run_id, now)
            )
            return run_id

    @staticmethod
    def _end_console_run(db: sqlite3.Connection, console_run_id: int, outcome: str, *, now: str) -> None:
        db.execute(
            "UPDATE console_run SET ended_at = ?, outcome = ? WHERE console_run_id = ? AND ended_at IS NULL",
            (now, outcome, console_run_id),
        )
        db.execute(
            "UPDATE console_slot SET console_run_id = NULL, held_since = NULL WHERE id = 1 AND console_run_id = ?",
            (console_run_id,),
        )

    def end_console_run(self, console_run_id: int, outcome: str, now: str) -> None:
        with self.transaction() as db:
            self._end_console_run(db, console_run_id, outcome, now=now)

    def slot(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT console_run.* FROM console_slot JOIN console_run USING(console_run_id) WHERE console_slot.id = 1"
        ).fetchone()
        return dict(row) if row else None

    def console_runs(self, unit_key: str | None = None) -> list[dict[str, Any]]:
        if unit_key is None:
            rows = self.connection.execute("SELECT * FROM console_run ORDER BY console_run_id")
        else:
            rows = self.connection.execute(
                "SELECT * FROM console_run WHERE unit_key = ? ORDER BY console_run_id", (unit_key,)
            )
        return [dict(row) for row in rows]

    def open_console_run_of(self, unit_key: str, kind: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM console_run WHERE unit_key = ? AND kind = ? AND ended_at IS NULL "
            "ORDER BY console_run_id DESC LIMIT 1",
            (unit_key, kind),
        ).fetchone()
        return dict(row) if row else None

    # ---- gate, disk, events ----------------------------------------------------------------------

    @staticmethod
    def _insert_gate(db: sqlite3.Connection, unit_key: str, point: str, verdict: dict[str, Any], *, now: str) -> None:
        db.execute(
            "INSERT INTO gate_verdict(unit_key, at, point, stage_arg, strict, outcome, exit_code, stage_reached, "
            "fail_ids_json, warn_ids_json, strict_hold_ids_json, blocking_fail_ids_json, run_policy_source, "
            "report_path, report_sha256, gate_commit, detail, blocking_unevaluated_ids_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                unit_key, now, point, verdict.get("stage", "all"), int(bool(verdict.get("strict", True))),
                verdict["outcome"], verdict.get("exit_code"), verdict.get("stage_reached"),
                _json(verdict.get("fail_ids") or []), _json(verdict.get("warn_ids") or []),
                _json(verdict.get("strict_hold_ids") or []), _json(verdict.get("blocking_fail_ids") or []),
                verdict.get("run_policy_source"), verdict.get("report_path"),
                verdict.get("report_sha256"), verdict.get("gate_commit") or "", verdict.get("detail"),
                _json(verdict.get("blocking_unevaluated_ids") or []),
            ),
        )

    def gate_verdicts(self, unit_key: str | None = None) -> list[dict[str, Any]]:
        if unit_key is None:
            rows = self.connection.execute("SELECT * FROM gate_verdict ORDER BY verdict_id")
        else:
            rows = self.connection.execute(
                "SELECT * FROM gate_verdict WHERE unit_key = ? ORDER BY verdict_id", (unit_key,)
            )
        return [dict(row) for row in rows]

    def disk_event(self, action: str, now: str, **values: Any) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO disk_event(at, volume, free_bytes, total_bytes, reserve_bytes, needed_bytes, unit_key, action) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now, values.get("volume", ""), values.get("free"), values.get("total"), values.get("reserve"),
                    values.get("need"), values.get("unit_key"), action,
                ),
            )

    def disk_events(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM disk_event ORDER BY seq")]

    def event(self, kind: str, now: str, detail: dict[str, Any] | None = None, unit_key: str | None = None) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO campaign_event(at, kind, unit_key, detail_json) VALUES (?, ?, ?, ?)",
                (now, kind, unit_key, _json(detail or {})),
            )

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            rows = self.connection.execute("SELECT * FROM campaign_event ORDER BY seq")
        else:
            rows = self.connection.execute("SELECT * FROM campaign_event WHERE kind = ? ORDER BY seq", (kind,))
        return [dict(row) for row in rows]

    # ---- the runner: lock, heartbeat, pause ------------------------------------------------------

    def runner(self) -> dict[str, Any]:
        return dict(self.connection.execute("SELECT * FROM runner WHERE id = 1").fetchone())

    def take_lock(
        self,
        pid: int,
        process_created_at: float | None,
        host: str,
        now: str,
        *,
        holder_alive: Callable[[dict[str, Any]], bool],
    ) -> tuple[bool, dict[str, Any]]:
        """Become the campaign's one runner, or say who is.

        A recorded holder counts while holder_alive says so; the caller decides with the process's
        creation time and the heartbeat's age, because Windows reuses process ids and os.kill(pid, 0) is
        not a probe there (it sends CTRL_C_EVENT).
        """
        with self.transaction() as db:
            current = dict(db.execute("SELECT * FROM runner WHERE id = 1").fetchone())
            if current.get("pid") and not (
                current["pid"] == pid and current.get("process_created_at") == process_created_at
            ) and holder_alive(current):
                return False, current
            db.execute(
                "UPDATE runner SET pid = ?, process_created_at = ?, host = ?, started_at = ?, heartbeat_at = ? WHERE id = 1",
                (pid, process_created_at, host, now, now),
            )
            db.execute(
                "INSERT INTO campaign_event(at, kind, detail_json) VALUES (?, 'runner_started', ?)",
                (now, _json({"pid": pid, "host": host, "previous": {k: current.get(k) for k in ("pid", "host", "heartbeat_at")}})),
            )
        return True, self.runner()

    def release_lock(self, pid: int, now: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE runner SET pid = NULL, process_created_at = NULL, heartbeat_at = ? WHERE id = 1 AND pid = ?",
                (now, pid),
            )

    def pause(self, kind: str, reason: str, now: str) -> None:
        """Pause the campaign, or say more about the pause it is in.

        A pause of the kind already in force keeps its paused_at and takes the newer reason: the hourly
        fault recheck counts from the first fault, not from the last job that could not be polled. A
        lesser pause does not replace a greater one (PAUSE_RANK).
        """
        with self.transaction() as db:
            current = dict(db.execute("SELECT * FROM runner WHERE id = 1").fetchone())
            if current["paused"] and current["pause_kind"] == kind:
                if current["pause_reason"] != reason:
                    db.execute("UPDATE runner SET pause_reason = ? WHERE id = 1", (reason,))
                return
            if current["paused"] and PAUSE_RANK.get(current["pause_kind"], 0) > PAUSE_RANK[kind]:
                return
            db.execute(
                "UPDATE runner SET paused = 1, pause_kind = ?, pause_reason = ?, paused_at = ? WHERE id = 1",
                (kind, reason, now),
            )
            db.execute(
                "INSERT INTO campaign_event(at, kind, detail_json) VALUES (?, 'paused', ?)",
                (now, _json({"pause_kind": kind, "reason": reason})),
            )

    def resume(self, now: str, kinds: Iterable[str] | None = None, detail: dict[str, Any] | None = None) -> bool:
        with self.transaction() as db:
            current = dict(db.execute("SELECT * FROM runner WHERE id = 1").fetchone())
            if not current["paused"] or (kinds is not None and current["pause_kind"] not in set(kinds)):
                return False
            db.execute(
                "UPDATE runner SET paused = 0, pause_kind = NULL, pause_reason = NULL, paused_at = NULL WHERE id = 1"
            )
            db.execute(
                "INSERT INTO campaign_event(at, kind, detail_json) VALUES (?, 'resumed', ?)",
                (now, _json({"was": {k: current[k] for k in ("pause_kind", "pause_reason")}, **(detail or {})})),
            )
        return True

    # ---- operator requests -------------------------------------------------------------------------

    def add_request(self, action: str, unit_key: str, reason: str, by: str, now: str) -> int:
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM unit WHERE unit_key = ?", (unit_key,)).fetchone() is None:
                raise LedgerError(f"Unknown unit {unit_key}.")
            cursor = db.execute(
                "INSERT INTO request(at, action, unit_key, reason, requested_by) VALUES (?, ?, ?, ?, ?)",
                (now, action, unit_key, reason, by),
            )
            return int(cursor.lastrowid)

    def pending_requests(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute("SELECT * FROM request WHERE handled_at IS NULL ORDER BY request_id")
        ]

    def handle_request(self, request_id: int, detail: str, now: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE request SET handled_at = ?, handled_detail = ? WHERE request_id = ? AND handled_at IS NULL",
                (now, detail, request_id),
            )


class Heartbeat:
    """Refreshes the runner's heartbeat from its own thread and connection.

    The raw-metadata preflight runs in the runner's process and can take hours for a unit of 165 Waters
    folders; without this the lock would read stale meanwhile and a second runner could take it.
    """

    def __init__(self, path: Path, pid: int, interval: float, clock: Callable[[], str]) -> None:
        self.path = path
        self.pid = pid
        self.interval = interval
        self.clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="campaign-heartbeat", daemon=True)

    def start(self) -> "Heartbeat":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        connection = sqlite3.connect(str(self.path), isolation_level=None, timeout=30.0)
        try:
            while not self._stop.wait(self.interval):
                try:
                    connection.execute(
                        "UPDATE runner SET heartbeat_at = ? WHERE id = 1 AND pid = ?", (self.clock(), self.pid)
                    )
                except sqlite3.Error:
                    pass
        finally:
            connection.close()
