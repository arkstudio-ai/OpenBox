"""Atomic, source-ordered profile commands shared by settings and extraction."""
from datetime import datetime, timezone
from hashlib import sha256
import json

from db.repository.preference_repo import locked_preference
from memory.versions import changed, previous_receipt, record_receipt

REVISION_KEY = "assistant_profile_revision"
SOURCE_KEY = "assistant_profile_source_keys"


def source_key(refs):
    """Stable human content identity across tool and extraction protocols."""
    identities = []
    for ref in refs:
        if ref.get("message_id") and ref.get("part_id") and ref.get("content_hash"):
            identities.append(["chat", ref["message_id"], ref["part_id"], ref["content_hash"]])
        elif ref.get("source_kind") == "native_voice_utterance" and ref.get("source_id"):
            identities.append(["voice", ref["source_id"]])
        else:
            return None
    return sha256(json.dumps(sorted(identities), ensure_ascii=False).encode()).hexdigest() if identities else None


def aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


async def apply_profile(db, *, user_id: str, patch: dict, operation_id: str,
                        via="settings", expected_revision=None, occurred_at=None,
                        source_refs=(), checked_source=False, field_times=None, field_sources=None) -> dict:
    """Caller locks actor first and revalidates any model proposal's evidence.

    A receipt is part of this transaction, not evidence that the caller has
    already committed it. Replaying a command never reapplies its old patch.
    """
    from assistant import profile
    profile.merge(profile.Profile(), patch)
    if via not in profile.VIA or not patch:
        raise ValueError("ASSISTANT_PROFILE_INVALID")
    if via == "chat" and not checked_source:
        raise ValueError("A conversational setting requires checked original evidence")
    payload = {"patch": patch, "via": via, "expected_revision": expected_revision if via != "chat" else None,
               "source_refs": list(source_refs), "occurred_at": aware(occurred_at).isoformat() if occurred_at else None,
               "field_times": {key: aware(value).isoformat() for key, value in (field_times or {}).items()}}
    old = await previous_receipt(db, user_id, operation_id, payload)
    if old is not None:
        return old
    row = await locked_preference(db, user_id)
    extra = dict(row.extra or {})
    current = profile.from_extra(extra)
    decided = profile.decided_from_extra(extra)
    revision = int(extra.get(REVISION_KEY) or 0)
    source_time = aware(occurred_at)
    source_keys = dict(extra.get(SOURCE_KEY) or {})
    incoming_keys = {key: source_key((field_sources or {}).get(key, source_refs)) for key in patch} if via == "chat" else {}
    repeated = {key for key in patch if incoming_keys.get(key)
        and source_keys.get(key) == incoming_keys[key]
        and aware(decided.get(key, {}).get("at")) == (aware((field_times or {}).get(key)) or source_time)}
    if repeated == set(patch):
        # Two writers can verify the same human decision. The later one must
        # not issue a new revision/event or normalize its wording a second time.
        from memory.versions import version
        result = {"state": "saved", "replayed": True, "revision": revision,
            "context_version": await version(db, user_id),
            "fields": {key: getattr(current, key) for key in patch}}
        return await record_receipt(db, user_id=user_id, operation_id=operation_id, kind="profile",
            payload=payload, result=result, source_refs=source_refs, occurred_at=source_time)
    conflict = expected_revision is not None and expected_revision != revision
    if source_time:
        for key in patch:
            decision = decided.get(key)
            # Unknown legacy provenance is not permission for an old job to
            # overwrite a direct decision. A new UI decision can still edit it.
            field_time = aware((field_times or {}).get(key)) or source_time
            if decision and (not decision.get("at") or aware(decision["at"]) > field_time):
                conflict = True
    if conflict:
        result = {"state": "conflict", "revision": revision, "reason": "newer_user_decision"}
    else:
        changed_patch = {key: value for key, value in patch.items() if key not in repeated}
        after = profile.merge(current, changed_patch)
        now = datetime.now(timezone.utc)
        # Reasserting a default is a new decision, even if the visible value
        # did not change. Its source time fences all older extraction jobs.
        decided.update({key: {"at": (aware((field_times or {}).get(key)) or source_time or now).isoformat(),
                              "via": via} for key in changed_patch})
        for key in changed_patch:
            if incoming_keys.get(key):
                source_keys[key] = incoming_keys[key]
            else:
                source_keys.pop(key, None)
        revision += 1
        extra.pop(profile.LEGACY_NAME_KEY, None)
        row.extra = {**extra,
                     profile.PREFERENCE_KEY: profile.stored_fields(extra, profile.PREFERENCE_KEY, after.as_dict()),
                     profile.DECIDED_KEY: profile.stored_fields(extra, profile.DECIDED_KEY, decided),
                     REVISION_KEY: revision, SOURCE_KEY: source_keys}
        context_version = await changed(db, user_id)
        result = {"state": "saved", "revision": revision, "context_version": context_version,
                  "fields": {key: getattr(after, key) for key in patch}}
    return await record_receipt(db, user_id=user_id, operation_id=operation_id, kind="profile",
        payload=payload, result=result, source_refs=source_refs, occurred_at=source_time)


async def apply_extracted_settings(db, frozen, proposals, grounding, *, expected_revision=None):
    from assistant.style import setting_patch
    from wiki_compiler.hashing import canonical_hash
    if not grounding or grounding.get("input_hash") != frozen.input_hash:
        return None
    patch, refs, field_times, field_sources = {}, [], {}, {}
    for proposal in proposals:
        change = setting_patch(proposal.get("fact_key"), proposal.get("setting"))
        if not change or canonical_hash(proposal) not in grounding.get("supported", []):
            continue
        dates, proposal_refs = [], []
        for index in proposal["source_indexes"]:
            source = frozen.sources[index]
            reference = {key: source[key] for key in ("message_id", "part_id", "content_hash", "source_revision")
                         if key in source}
            if source.get("source_kind") == "native_voice_utterance":
                # There is deliberately no chat Message/Part for native ASR.
                # Keep its checked immutable source identity in the decision receipt.
                reference.update(source_id=source["id"], source_kind=source["source_kind"],
                                 session_id=source["session_id"])
            refs.append(reference)
            proposal_refs.append(reference)
            if source.get("occurred_at"):
                dates.append(aware(source["occurred_at"]))
        if dates:
            for key, value in change.items():
                moment = max(dates)
                if key not in field_times or moment >= field_times[key]:
                    patch[key], field_times[key], field_sources[key] = value, moment, proposal_refs
    if not patch:
        return None
    return await apply_profile(db, user_id=frozen.user_id, patch=patch,
        operation_id=f"extraction-settings:{frozen.job_id}", via="chat",
        expected_revision=expected_revision, occurred_at=max(field_times.values()),
        field_times=field_times, field_sources=field_sources, source_refs=refs, checked_source=True)
