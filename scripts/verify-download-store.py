"""Check an accession's download store against the units that use it.

Interactive's download store (msdial_app/download_store.py, 0.5.13) fetches each repository object once
per accession and gives every unit that needs it a tree of hardlinks:

    <workspace_root>/<repository>/<accession>/_dl/
        index/<url key>.json            URL -> current object id, and the ids it pointed at before
        partial/<url key>[.part|.json]  a resumable transfer and who started it
        o/<object id>/obj/<name>        the bytes
        o/<object id>/t/                their verified extraction
        o/<object id>/entry.json        the object record; a tombstone once collected
        o/<object id>/members.tsv       size and mtime_ns of every file units link to
        claims/<url key>/<unit>.json    one consumer's claim: pending, materialized or released
        locks/<name>.lock               exclusive-create locks kept fresh by a heartbeat

The store deletes nothing on its own. An object goes only when gc runs under a campaign approval that
covers boundary 5 for every unit whose released claim freed it, and it leaves entry.json behind as a
tombstone. What that rule leaves for a person to look at is what this script finds:

- orphan objects: bytes in o/ that no readable record describes, or that no index and no claim reaches;
- live claims on terminal units: a claim still pending or materialized for a unit whose raw tree is
  released (raw_cleaned, discarded, or its split parent's raw_release deleted), opened before that
  deletion, which keeps the object for ever. One opened after it is a new consumer's, a re-run's
  pre-claim say, and is listed for information; so is one whose release is under way (its c-<key> lock
  fresh, released by the time it is read again, or a deletion recorded within a lock's heartbeat window);
- objects with no releasing unit: bytes that no claim names at all, which gc keeps for ever, because no
  approval can name the unit they belong to;
- tombstones: collected objects, each listed; one whose bytes are still there, or that names no approval
  covering boundary 5, is refused.

It also warns about abandoned partial transfers, objects modified in place since they were recorded,
indexes and claims that point at no object, and locks whose heartbeat has lapsed.

Nothing here writes, moves or deletes anything. It is safe to run while a campaign runs. A file that
goes between the listing and the stat is not counted. The store changes an object only under its
o-<id> lock and points a URL at one only under the URL's u-<key> lock, so what looks wrong with an object
while one of them is fresh - bytes an install has placed and not yet recorded, say - is reported as
object_busy, for information, and so is a condition gone by the time it is looked at again. A record
being replaced at that instant is reported as unreadable, and a second run settles it.

Usage:
    python scripts/verify-download-store.py <store | accession directory | workspace root> [--json]

    A store is a directory named _dl. An accession directory holds one; a workspace root holds
    <repository>/<accession>/_dl for every accession that has one.

Exit codes: 0 nothing refused, 2 at least one finding refused, 3 no store was found.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path, PurePosixPath

GATE = Path(__file__).resolve().parent / "verify-run-invariants.py"

FAIL = "fail"
WARN = "warn"
INFO = "info"
SEVERITIES = (FAIL, WARN, INFO)

# download_store.py: the records, the claim states that keep an object, and the heartbeat past which a
# lock's holder has stopped refreshing it (whether that holder is alive is not judged here: reading a
# process's liveness is the store's business, and a lapsed heartbeat is already worth a look).
OBJECT_SCHEMA = "msdial-download-store-object.v1"
COLLECTED = "collected"
COLLECTION_INCOMPLETE = "collection_incomplete"
READY = "ready"
LOCK_STALE_SECONDS = 600.0
# A unit's deletion is recorded and then its claims are released; a live claim of a deletion younger than a
# lock's heartbeat window may be that release still under way.
RELEASE_GRACE_SECONDS = LOCK_STALE_SECONDS
MEMBERS_HEADER = ("path", "size", "crc", "mtime_ns")
# members.tsv escapes a backslash, a tab and a line break in a member's path (download_store._escape_tsv).
_ESCAPED = re.compile(r"\\[\\tnr]")
_UNESCAPED = {"\\\\": "\\", "\\t": "\t", "\\n": "\n", "\\r": "\r"}
_LISTED = 20
# Where an object directory holds bytes: the object, its extraction, and an extraction under way.
OBJECT_PARTS = ("obj", "t", "t.partial")


def _gate():
    spec = importlib.util.spec_from_file_location("verify_run_invariants", GATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("verify_run_invariants", module)
    spec.loader.exec_module(module)
    return module


gate = _gate()


def _children(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []


def _records(directory: Path) -> list[Path]:
    """The records in one directory; an atomic write's temporary file (a leading dot) is not one."""
    return [path for path in _children(directory) if path.suffix == ".json" and not path.name.startswith(".")]


def _has_files(root: Path) -> bool:
    return root.is_dir() and any(item.is_file() for item in root.rglob("*"))


def _tree_size(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(details.st_size for item in root.rglob("*") if item.is_file()
               for details in [gate._stat_quietly(item)] if details is not None)


def _pointed_ids(index: dict) -> set:
    """The object ids an index record points at now or pointed at before."""
    ids = {index.get("object_id")} | {item.get("object_id") for item in index.get("history") or []
                                      if isinstance(item, dict)}
    return {str(object_id) for object_id in ids if object_id}


def _members(path: Path) -> "list[dict] | None":
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or tuple(lines[0].split("\t")) != MEMBERS_HEADER:
        return None
    rows = []
    for line in lines[1:]:
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            return None
        member = _ESCAPED.sub(lambda match: _UNESCAPED[match.group(0)], parts[0])
        try:
            rows.append({"path": member, "size": int(parts[1]), "mtime_ns": int(parts[3])})
        except ValueError:
            return None
    return rows


def _changed_members(directory: Path) -> "list[str] | None":
    """The members of an object whose size or mtime_ns differ from members.tsv; None when it is unreadable."""
    rows = _members(directory / "members.tsv")
    if rows is None:
        return None
    changed = []
    for row in rows:
        details = gate._stat_quietly(directory.joinpath(*PurePosixPath(row["path"]).parts))
        if details is None:
            changed.append(f"{row['path']} (missing)")
        elif (details.st_size, details.st_mtime_ns) != (row["size"], row["mtime_ns"]):
            changed.append(row["path"])
    return changed


def _consumers(object_id: str, keys: "list[str]", indexes: dict, claims: dict) -> "tuple[list, list]":
    """(live, releasing): the claims on the object's URLs that keep it, and those that could release it."""
    on_urls = [record for key in keys for record in claims.get(key, [])]
    live = [record for record in on_urls if record.get("state") in gate.STORE_LIVE_CLAIM_STATES
            and (record.get("object_id") == object_id
                 or (not record.get("object_id") and (indexes.get(str(record.get("url_key"))) or {}).get("object_id")
                     == object_id))]
    releasing = [record for record in on_urls if record.get("object_id") in (None, "", object_id)]
    return live, releasing


class StoreCheck:
    def __init__(self, store: Path) -> None:
        self.store = store
        self.accession = store.parent
        self.findings: list[dict] = []

    def add(self, severity: str, kind: str, detail: str, **where) -> None:
        self.findings.append({"severity": severity, "kind": kind, "detail": detail, **where})

    def _held(self, names: "list[str]") -> str:
        """The first of these store locks whose heartbeat is fresh now, or ""."""
        now = time.time()
        for name in names:
            details = gate._stat_quietly(self.store / "locks" / f"{name}.lock")
            if details is not None and now - details.st_mtime <= LOCK_STALE_SECONDS:
                return name
        return ""

    def _busy(self, object_id: str, kind: str, still, keys: "list[str]" = ()) -> bool:
        """Whether what looked like `kind` on this object is the store at work rather than a finding.

        The lock is looked at after the condition was seen, and the condition (`still`) again after that,
        so work that ended in between is not reported either. True means an INFO was recorded instead.
        """
        held = self._held([f"o-{object_id}"] + [f"u-{key}" for key in keys])
        if not held and still():
            return False
        why = f"lock {held} is fresh" if held else "it changed as it was read"
        self.add(INFO, "object_busy", f"Object {object_id} was being worked on while it was checked ({why}), so what "
                                      f"looked like {kind} is left for a later run to judge.",
                 object_id=object_id, deferred=kind, lock=held or None)
        return True

    def _records_now(self, keys: "list[str]") -> "tuple[dict, dict]":
        """The index and claim records of these URLs as they are now, read again."""
        indexes: dict[str, dict] = {}
        claims: dict[str, list[dict]] = {}
        for key in keys:
            record, _ = gate._read_json(self.store / "index" / f"{key}.json")
            if record is not None:
                indexes[key] = record
            claims[key] = [record for path in _records(self.store / "claims" / key)
                           for record in [gate._read_json(path)[0]] if record is not None]
        return indexes, claims

    def _read(self, path: Path, what: str, **where) -> "dict | None":
        record, reason = gate._read_json(path)
        if record is None:
            self.add(FAIL, "unreadable_record", f"The {what} {path.relative_to(self.store)} {reason.split(' ', 1)[-1]}.",
                     **where)
        return record

    # ---- the units the claims name -----------------------------------------------------------------

    def _released(self, unit_id: str) -> "tuple[str, str, str]":
        """(state, why, deleted_at) of the unit a claim names: released, unknown (no workspace) or active.

        deleted_at is the time the deletion's record gives, "" when it gives none (the runner's record).
        """
        workspace = self.accession / unit_id
        manifest, reason = gate._read_json(workspace / "provenance" / "run-manifest.json")
        if manifest is None:
            return "unknown", reason, ""
        if isinstance(manifest.get("split_from"), dict):
            owner, _ = gate._raw_owner_manifest(manifest)
        else:
            owner = manifest
        kind, where, deleted_at = gate._recorded_deletion(manifest, owner or manifest)
        if kind:
            return "released", where, deleted_at
        record, _ = gate._read_json(workspace / gate.CAMPAIGN_RECORD_FILE)
        if record is not None and record.get("schema") == gate.CAMPAIGN_RECORD_SCHEMA \
                and str(record.get("raw_disposition") or "") in ("released", "discarded"):
            return "released", f"the campaign runner's record ({record.get('raw_disposition')})", ""
        return "active", str(manifest.get("status") or ""), ""

    # ---- the check ---------------------------------------------------------------------------------

    def run(self) -> dict:
        indexes: dict[str, dict] = {}
        for path in _records(self.store / "index"):
            record = self._read(path, "index record", url_key=path.stem)
            if record is not None:
                indexes[path.stem] = record
        claims: dict[str, list[dict]] = {}
        for directory in _children(self.store / "claims"):
            if not directory.is_dir():
                continue
            for path in _records(directory):
                record = self._read(path, "claim", url_key=directory.name)
                if record is not None:
                    claims.setdefault(directory.name, []).append(record)
        objects = [path for path in _children(self.store / "o") if path.is_dir()]
        object_ids = {path.name for path in objects}
        pointed: dict[str, set] = {}
        for key, index in indexes.items():
            for object_id in [index.get("object_id")] + [item.get("object_id") for item in index.get("history") or []
                                                         if isinstance(item, dict)]:
                if object_id:
                    pointed.setdefault(str(object_id), set()).add(key)
            current = str(index.get("object_id") or "")
            if current and current not in object_ids:
                self.add(WARN, "dangling_index", f"The index for {index.get('url') or key} points at object {current}, "
                                                  "which the store does not hold.", url_key=key, object_id=current)
        claimed: dict[str, list[dict]] = {}
        for key, records in claims.items():
            for record in records:
                if record.get("object_id"):
                    claimed.setdefault(str(record["object_id"]), []).append(record)

        tombstones = []
        states: Counter = Counter()
        for directory in objects:
            self._object(directory, indexes, claims, pointed, claimed, tombstones, states)
        self._claims(claims, object_ids)
        self._partials(claims)
        self._locks()
        counts = Counter(item["severity"] for item in self.findings)
        return {
            "store": str(self.store),
            "objects": len(objects),
            "object_states": dict(states),
            "claims": dict(Counter(str(record.get("state") or "unrecorded")
                                   for records in claims.values() for record in records)),
            "tombstones": tombstones,
            "counts": {severity: counts.get(severity, 0) for severity in SEVERITIES},
            "findings": self.findings,
        }

    def _object(self, directory: Path, indexes: dict, claims: dict, pointed: dict, claimed: dict,
                tombstones: list, states: Counter) -> None:
        object_id = directory.name
        holds = {part: _has_files(directory / part) for part in OBJECT_PARTS}
        has_bytes = any(holds.values())
        size = sum(_tree_size(directory / part) for part, held in holds.items() if held)
        entry_path = directory / "entry.json"
        if not entry_path.is_file():
            states["no_record"] += 1
            if has_bytes:
                # An install places the bytes and then writes entry.json, both under o-<id>.
                if not self._busy(object_id, "orphan_object", lambda: not entry_path.is_file()):
                    self.add(FAIL, "orphan_object", f"Object {object_id} holds {size:,} bytes and no entry.json, so "
                                                     "nothing says what they are or who uses them.",
                             object_id=object_id, bytes=size)
            else:
                self.add(WARN, "incomplete_object", f"Object directory {object_id} holds neither bytes nor a record.",
                         object_id=object_id)
            return
        entry = self._read(entry_path, "object record", object_id=object_id)
        if entry is None:
            states["unreadable"] += 1
            return
        state = str(entry.get("state") or "unrecorded")
        states[state] += 1
        if entry.get("schema") != OBJECT_SCHEMA or str(entry.get("object_id") or "") != object_id:
            self.add(FAIL, "orphan_object", f"Object {object_id}'s record is not an object record for it "
                                             f"(schema {entry.get('schema')!r}, object_id {entry.get('object_id')!r}).",
                     object_id=object_id, bytes=size)
            return
        name = str(entry.get("name") or "")
        if state == COLLECTED:
            under = entry.get("collected_under") if isinstance(entry.get("collected_under"), dict) else {}
            tombstones.append({"object_id": object_id, "name": name, "collected_at": entry.get("collected_at"),
                               "approval_id": under.get("approval_id"), "collected_bytes": entry.get("collected_bytes"),
                               "released_by": sorted({str(item.get("unit_id") or "") for item in
                                                      entry.get("release_record") or [] if isinstance(item, dict)})})
            # A tombstone installed again gets its bytes before its record says ready, under o-<id>.
            if has_bytes and not self._busy(object_id, "tombstone_with_bytes",
                                            lambda: self._state_now(entry_path) == COLLECTED):
                self.add(FAIL, "tombstone_with_bytes", f"Object {object_id} ({name}) is recorded as collected at "
                                                        f"{entry.get('collected_at')}, and {size:,} bytes of it remain.",
                         object_id=object_id, bytes=size)
            if not under.get("approval_id") or str(under.get("boundary")) != gate.RAW_DELETION_BOUNDARY:
                self.add(FAIL, "tombstone_without_approval",
                         f"Object {object_id} ({name}) was collected, and its tombstone names no campaign approval "
                         "covering boundary 5: nothing records the authority its bytes were deleted under.",
                         object_id=object_id)
            return
        if state == COLLECTION_INCOMPLETE:
            kept = entry.get("collection_kept") or []
            self.add(WARN, "collection_incomplete", f"Object {object_id} ({name}) was collected in part: "
                                                     f"{len(kept)} file(s) could not be removed ({kept[:3]}).",
                     object_id=object_id, bytes=size)
        keys = sorted({str(item.get("url_key")) for item in entry.get("urls") or []
                       if isinstance(item, dict) and item.get("url_key")})
        live, releasing = _consumers(object_id, keys, indexes, claims)
        reached = object_id in pointed or object_id in claimed
        if has_bytes and not (reached and (live or releasing)):
            # The records were read before this object was. A fetch installs an object, then points its
            # URL's index at it and records its claim, all under u-<key>: read them again once it is free.
            def still() -> bool:
                nonlocal reached, live, releasing
                fresh_indexes, fresh_claims = self._records_now(keys)
                reached = reached or any(object_id in _pointed_ids(index) for index in fresh_indexes.values()) \
                    or any(str(record.get("object_id") or "") == object_id
                           for records in fresh_claims.values() for record in records)
                live, releasing = _consumers(object_id, keys, fresh_indexes, fresh_claims)
                return not (reached and (live or releasing))

            if self._busy(object_id, "no_releasing_unit" if reached else "orphan_object", still, keys):
                return
        if has_bytes and not reached:
            self.add(FAIL, "orphan_object", f"Object {object_id} ({name}, {size:,} bytes) is reached by no index and "
                                             "named by no claim: nothing will ever link to it or release it.",
                     object_id=object_id, bytes=size)
            return
        if has_bytes and not live and not releasing:
            self.add(FAIL, "no_releasing_unit", f"Object {object_id} ({name}, {size:,} bytes) is claimed by no unit, "
                                                 "live or released, so no approval can name a unit to collect it for: "
                                                 "gc keeps it for ever.", object_id=object_id, bytes=size)
        if state == READY:
            missing = lambda: not (directory / "obj" / name).is_file()  # noqa: E731
            # gc unlinks a ready object's files before it writes the tombstone, under o-<id>.
            if missing() and not self._busy(object_id, "object_file_missing", missing):
                self.add(WARN, "object_file_missing", f"Object {object_id} is recorded as ready, and obj/{name} is "
                                                       "not there.", object_id=object_id)
            self._members(directory, object_id, name)

    @staticmethod
    def _state_now(entry_path: Path) -> str:
        record, _ = gate._read_json(entry_path)
        return str((record or {}).get("state") or "")

    def _members(self, directory: Path, object_id: str, name: str) -> None:
        changed = _changed_members(directory)
        # A re-extraction rewrites the tree and then members.tsv, and gc unlinks the files, under o-<id>.
        if changed != [] and self._busy(object_id, "unreadable_members" if changed is None else "modified_in_place",
                                        lambda: _changed_members(directory) != []):
            return
        if changed is None:
            self.add(WARN, "unreadable_members", f"Object {object_id}'s members.tsv cannot be read, so whether a unit "
                                                  "wrote into its files in place is not established.", object_id=object_id)
            return
        if changed:
            self.add(WARN, "modified_in_place", f"{len(changed)} file(s) of object {object_id} ({name}) differ from "
                                                 f"their recorded size and mtime: {', '.join(changed[:3])}. A hardlink is "
                                                 "the store's own file, so a reader that wrote into a linked file "
                                                 "changed every unit's copy.", object_id=object_id, members=changed[:_LISTED])

    def _being_released(self, key: str, unit_id: str, deleted_at: str, where: dict) -> bool:
        """Whether a live claim of a unit whose tree is released is that release under way, not a leftover.

        The deletion is recorded first, and then each of the unit's claims is released under c-<key>. The
        lock is looked at after the claim was seen and the claim read again after that, and a deletion
        recorded within a lock's heartbeat window may not have reached its release yet. True means an INFO
        was recorded instead.
        """
        held = self._held([f"c-{key}"])
        why = f"lock {held} is fresh" if held else ""
        if not why:
            _, fresh = self._records_now([key])
            if not any(str(item.get("unit_id") or "") == unit_id and item.get("state") in gate.STORE_LIVE_CLAIM_STATES
                       for item in fresh.get(key, [])):
                why = "it was released as it was read"
        if not why:
            deleted = gate._instant(deleted_at) if deleted_at else None
            age = time.time() - deleted.timestamp() if deleted is not None else None
            if age is not None and 0 <= age <= RELEASE_GRACE_SECONDS:
                why = f"the deletion was recorded {age:.0f} s ago, and its release may not have reached this claim yet"
        if not why:
            return False
        self.add(INFO, "object_busy", f"Unit {unit_id}'s claim on {key} was being released while it was checked ({why}), "
                                      "so what looked like live_claim_on_terminal_unit is left for a later run to judge.",
                 deferred="live_claim_on_terminal_unit", lock=held or None, **where)
        return True

    def _claims(self, claims: dict, object_ids: set) -> None:
        verdicts: dict[str, tuple[str, str, str]] = {}
        pre_claimed: list[str] = []
        reopened: list[str] = []
        for key, records in claims.items():
            for record in records:
                unit_id = str(record.get("unit_id") or "")
                state = str(record.get("state") or "")
                if state not in gate.STORE_LIVE_CLAIM_STATES:
                    continue
                if unit_id not in verdicts:
                    verdicts[unit_id] = (self._released(unit_id) if unit_id
                                         else ("unknown", "the claim names no unit", ""))
                released, why, deleted_at = verdicts[unit_id]
                where = {"url_key": key, "unit_id": unit_id, "object_id": record.get("object_id")}
                if released == "released":
                    # A claim opened after the deletion is a new consumer's: download_store.claim reopens a
                    # released claim, as a batch pre-claim for a re-run of the unit does.
                    if gate._claim_opened_after(record, deleted_at):
                        reopened.append(unit_id)
                    elif not self._being_released(key, unit_id, deleted_at, where):
                        self.add(FAIL, "live_claim_on_terminal_unit",
                                 f"Unit {unit_id}'s claim on {record.get('url') or key} is still {state}, and the unit's "
                                 f"raw tree is released ({why}): the store keeps the object for a consumer that has gone.",
                                 **where)
                elif released == "unknown":
                    if state == "materialized":
                        self.add(WARN, "live_claim_without_unit",
                                 f"Unit {unit_id} has a materialized claim on {record.get('url') or key} and no readable "
                                 f"manifest ({why}), so whether its tree still links the object is not established.",
                                 **where)
                    else:
                        pre_claimed.append(unit_id)
                if record.get("object_id") and str(record["object_id"]) not in object_ids:
                    self.add(WARN, "dangling_claim", f"Unit {unit_id}'s {state} claim names object {record['object_id']}, "
                                                      "which the store does not hold.", **where)
        if pre_claimed:
            units = sorted(set(pre_claimed))
            self.add(INFO, "pre_claimed", f"{len(pre_claimed)} pending claim(s) of {len(units)} unit(s) with no workspace "
                                           f"yet ({', '.join(units[:5])}): pre-claims for units that have not started.",
                     units=units[:_LISTED])
        if reopened:
            units = sorted(set(reopened))
            self.add(INFO, "claimed_after_release",
                     f"{len(reopened)} live claim(s) of {len(units)} unit(s) whose raw tree is released were opened after "
                     f"the deletion ({', '.join(units[:5])}): new consumers, such as a re-run's pre-claim, not leftovers.",
                     units=units[:_LISTED])

    def _partials(self, claims: dict) -> None:
        keys = sorted({path.name.split(".", 1)[0] for path in _children(self.store / "partial")
                       if path.is_file() and not path.name.startswith(".")})
        for key in keys:
            live = [record for record in claims.get(key, []) if record.get("state") in gate.STORE_LIVE_CLAIM_STATES]
            if live:
                continue
            size = sum(details.st_size for path in _children(self.store / "partial")
                       if path.is_file() and path.name.split(".", 1)[0] == key and path.suffix != ".json"
                       for details in [gate._stat_quietly(path)] if details is not None)
            self.add(WARN, "abandoned_partial", f"The partial transfer {key} ({size:,} bytes) has no live claim on its URL; "
                                                 "gc removes it once an approval covers the units that released it.",
                     url_key=key, bytes=size)

    def _locks(self) -> None:
        now = time.time()
        for path in _children(self.store / "locks"):
            details = gate._stat_quietly(path) if path.suffix == ".lock" else None
            if details is None:
                # Not a lock, or one released between the listing and the stat: a c-<key> lock comes and
                # goes around every claim write.
                continue
            age = now - details.st_mtime
            if age > LOCK_STALE_SECONDS:
                self.add(WARN, "stale_lock", f"Lock {path.name}'s heartbeat lapsed {age / 60:.0f} minutes ago. Its holder "
                                              "has stopped refreshing it; the store breaks it once the holder is dead.",
                         lock=path.name)


def stores_under(path: Path) -> list[Path]:
    if path.name == gate.STORE_DIRECTORY and path.is_dir():
        return [path]
    if (path / gate.STORE_DIRECTORY).is_dir():
        return [path / gate.STORE_DIRECTORY]
    return sorted(item for item in path.glob(f"*/*/{gate.STORE_DIRECTORY}") if item.is_dir())


def render(results: list[dict]) -> str:
    lines = []
    for result in results:
        lines.append(f"STORE {result['store']}: {result['objects']} object(s) {result['object_states']}, "
                     f"claims {result['claims']}, {len(result['tombstones'])} tombstone(s)")
        for item in result["findings"]:
            lines.append(f"  [{item['severity'].upper()}] {item['kind']}: {item['detail']}")
        for item in result["tombstones"][:_LISTED]:
            lines.append(f"  [TOMB] {item['object_id']} {item['name']} collected {item['collected_at']} under "
                         f"{item['approval_id'] or 'no approval'} for {', '.join(item['released_by']) or 'no unit'}")
    failed = sum(result["counts"][FAIL] for result in results)
    lines.append(f"VERDICT: {'refused' if failed else 'ok'} ({failed} refused, "
                 f"{sum(result['counts'][WARN] for result in results)} to look at)")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("path", type=Path, help="a store (_dl), an accession directory or a workspace root")
    parser.add_argument("--json", action="store_true", help="emit the full result as JSON")
    arguments = parser.parse_args(argv)
    stores = stores_under(arguments.path)
    if not stores:
        print(f"No download store ({gate.STORE_DIRECTORY}) was found at or under {arguments.path}.", file=sys.stderr)
        return 3
    results = [StoreCheck(store).run() for store in stores]
    # Findings quote object names, URLs and unit ids, and a piped stdout takes the console code page
    # (cp1252 here, cp932 on a Japanese Windows): a name it cannot encode would end the check with a
    # traceback and exit 1, which is no verdict. JSON escapes what it cannot print; text replaces it.
    if arguments.json:
        print(json.dumps({"ok": not any(result["counts"][FAIL] for result in results), "stores": results},
                         indent=2, ensure_ascii=True))
    else:
        try:
            sys.stdout.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
        print(render(results))
    return 2 if any(result["counts"][FAIL] for result in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
