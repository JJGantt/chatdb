#!/usr/bin/env python3
"""Read and write macOS Messages, straight out of chat.db.

Read path is a direct read-only SQLite open of ~/Library/Messages/chat.db.
Two things about that database drive most of the code here:

  * `message.text` is NULL on roughly a third of rows. The real content lives in
    `attributedBody`, an NSAttributedString serialised with the old typedstream
    (NSArchiver) format. We decode it through Apple's own NSUnarchiver rather
    than scraping the blob, so a format change is Apple's problem, not ours.
  * Senders are `handle` rows holding raw phone numbers / emails, never names.
    Names are merged from the Mac's local AddressBook and Google Contacts,
    matched on a normalised phone number, because neither source is complete on
    its own.

Send path is AppleScript, which is the only way to send as the user's own
identity. Message text and file paths are passed as argv arguments, never
interpolated into the script source.

Requires Full Disk Access for whatever process runs this.
"""

import contextlib
import functools
import glob
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import objc
from Foundation import NSData, NSUnarchiver

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
ADDRESSBOOK_GLOB = os.path.expanduser(
    "~/Library/Application Support/AddressBook/**/*.abcddb"
)
# Google Contacts is read with the refresh token gmail-mcp already stores, so the
# secret lives in exactly one place. Override with GOOGLE_CREDS_ENV.
GOOGLE_ENV = pathlib.Path(os.environ.get(
    "GOOGLE_CREDS_ENV", os.path.expanduser("~/repos/gmail-mcp/.env")))

# Cocoa epoch: 2001-01-01. Modern rows store nanoseconds since then, older rows
# store seconds; anything past this threshold is nanoseconds.
APPLE_EPOCH_OFFSET = 978307200
NANOSECOND_THRESHOLD = 10**11



# ---------------------------------------------------------------- helpers


def _query(sql, params=()):
    """Every row the query returns, with the connection closed afterwards.

    A fresh connection per read is deliberate: chat.db belongs to Messages, which
    writes to it constantly, and opening one each time is the simplest way to be
    sure a read sees the latest committed rows.

    Closing it is not optional. An unclosed sqlite3.Connection is kept alive by
    reference cycles rather than freed by refcounting, so every read that drops
    one on the floor leaks its descriptors and its page cache over a 150 MB
    database until the cyclic collector happens to run. Measured 2026-09-17:
    about ten leaked descriptors a minute, 3.9 GB resident after 22 hours, and an
    8 GB Mac that filled its swap and froze. See the README.

    The connection is opened here and nowhere else, so no read can hold one open.
    """
    with contextlib.closing(sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)) as conn:
        return conn.execute(sql, params).fetchall()


def _query_one(sql, params=()):
    """The first row the query returns, or None. Closes the connection."""
    rows = _query(sql, params)
    return rows[0] if rows else None


def _apple_time(value):
    """Apple timestamp -> local ISO string, or None."""
    if not value:
        return None
    seconds = value / 1e9 if value > NANOSECOND_THRESHOLD else value
    stamp = datetime.fromtimestamp(seconds + APPLE_EPOCH_OFFSET, tz=timezone.utc)
    return stamp.astimezone().isoformat(timespec="seconds")


def _decode_body(blob):
    """Decode an attributedBody blob to plain text via Apple's unarchiver."""
    if not blob:
        return None
    # Every object the bridge makes here is autoreleased, and this process has no
    # Cocoa run loop to drain the pool, so without an explicit one they accumulate
    # for the life of the process. Measured 2026-09-17: 3,000 decodes leak 3.8 MB
    # unpooled against 0.4 MB pooled, ~1.3 KB each. JaredBot decodes ~460 blobs on
    # every kqueue wakeup, which is the ~3 MB/min it still climbed after the sqlite
    # connections were fixed. The str() copies are Python objects and outlive the pool.
    try:
        with objc.autorelease_pool():
            data = NSData.dataWithBytes_length_(blob, len(blob))
            obj = NSUnarchiver.unarchiveObjectWithData_(data)
            if obj is None:
                return None
            return str(obj.string()) if hasattr(obj, "string") else str(obj)
    except Exception:
        return None


def _body(text, attributed_body):
    """Message content, preferring the plain column and falling back to the blob."""
    if text:
        return text
    return _decode_body(attributed_body)


# A tapback is a message row, not a property of the message it reacts to. Its text
# reads like a sentence ("Loved ..."), which is indistinguishable from someone
# typing that, and it quotes the original — so an unstructured reader both
# miscounts messages and double-counts the quoted content.
REACTIONS = {2000: "loved", 2001: "liked", 2002: "disliked",
             2003: "laughed", 2004: "emphasized", 2005: "questioned"}


def _reaction(assoc_type):
    """(kind, removed) for a tapback row, else (None, False).

    The 3000 series is the same reaction being taken back, which matters: without
    it a removal reads as though someone reacted, not un-reacted.
    """
    if assoc_type in REACTIONS:
        return REACTIONS[assoc_type], False
    if assoc_type - 1000 in REACTIONS:
        return REACTIONS[assoc_type - 1000], True
    if assoc_type in (2006, 2007):
        return "emoji", False
    return None, False


def _assoc_guid(raw):
    """Target message guid out of an association like 'p:0/<GUID>'."""
    if not raw:
        return None
    return raw.rsplit("/", 1)[-1] or None


def _normalise(handle):
    """Reduce a phone number to comparable digits; leave emails alone."""
    if not handle:
        return ""
    if "@" in handle:
        return handle.strip().lower()
    digits = "".join(c for c in handle if c.isdigit())
    # US numbers appear both with and without the country code.
    return digits[-10:] if len(digits) >= 10 else digits


def _addressbook_contacts() -> dict:
    """handle -> (name, handle as written) from the Mac's local AddressBook."""
    names = {}
    for path in glob.glob(ADDRESSBOOK_GLOB, recursive=True):
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        for table, column in (
            ("ZABCDPHONENUMBER", "ZFULLNUMBER"),
            ("ZABCDEMAILADDRESS", "ZADDRESS"),
        ):
            try:
                rows = db.execute(
                    f"""select r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, v.{column}
                        from {table} v join ZABCDRECORD r on v.ZOWNER = r.Z_PK"""
                ).fetchall()
            except sqlite3.Error:
                continue
            for first, last, org, value in rows:
                name = " ".join(p for p in (first, last) if p) or org
                key = _normalise(value)
                if name and key:
                    names.setdefault(key, (name, value.strip()))
        db.close()
    return names


def _google_contacts() -> dict:
    """handle -> (name, handle as written) from Google Contacts, or {} if
    credentials aren't available.

    Reuses the refresh token gmail-mcp already holds rather than storing a second
    copy of the same secret. Absent or broken credentials degrade to the local
    address book instead of failing, so this server still works standalone.

    Absent and broken are not the same fault, though, and only one is worth
    reporting. No .env means Google was never configured. A .env whose token
    Google rejects means every Google-only name has just disappeared from every
    tool here — on this Mac the local address book holds 29 entries against
    Google's ~195, so the visible symptom is numbers where names used to be, with
    nothing saying why. That happened: a Google password change revoked the
    shared refresh token and the resulting invalid_grant was swallowed here.
    The rejected case now lands in _google_error, is logged to stderr, and is
    reported by list_contacts.
    """
    global _google_error
    _google_error = None
    if not GOOGLE_ENV.exists():
        return {}
    try:
        from dotenv import dotenv_values
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        return {}

    cfg = dotenv_values(GOOGLE_ENV)
    if not cfg.get("GOOGLE_REFRESH_TOKEN"):
        return {}

    try:
        creds = Credentials(
            token=None,
            refresh_token=cfg["GOOGLE_REFRESH_TOKEN"],
            client_id=cfg["GOOGLE_CLIENT_ID"],
            client_secret=cfg["GOOGLE_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            # No scopes pinned: naming one here puts it in the refresh request,
            # and Google rejects a refresh asking for any scope the token was not
            # granted. Pinning contacts.readonly broke this the moment gmail-mcp
            # re-authorized as contacts. The token carries its own grant, and
            # nothing below writes.
            scopes=None,
        )
        creds.refresh(Request())
        people = build("people", "v1", credentials=creds, cache_discovery=False)

        names, token = {}, None
        while True:
            page = people.people().connections().list(
                resourceName="people/me", pageSize=1000,
                personFields="names,phoneNumbers,emailAddresses",
                pageToken=token).execute()
            for person in page.get("connections", []):
                name = next((n.get("displayName")
                             for n in person.get("names", [])), None)
                if not name:
                    continue
                for phone in person.get("phoneNumbers", []):
                    raw = (phone.get("canonicalForm") or phone.get("value", "")).strip()
                    key = _normalise(raw)
                    if key:
                        names.setdefault(key, (name, raw))
                for email in person.get("emailAddresses", []):
                    raw = email.get("value", "").strip()
                    key = _normalise(raw)
                    if key:
                        names.setdefault(key, (name, raw))
            token = page.get("nextPageToken")
            if not token:
                return names
    except Exception as exc:
        _google_error = f"{type(exc).__name__}: {exc}"
        print(f"messages-mcp: Google Contacts unavailable, falling back to the "
              f"local address book only ({_google_error})", file=sys.stderr)
        return {}


_contacts_cache = None
_contacts_fingerprint = None
_sources_cache: dict = {}
# Normalised key -> the handle as the contact card writes it. Anything that sends
# must use this and never the key: _normalise keeps only the last ten digits,
# which is fine for comparing but drops the country code of a non-US number.
_card_handles: dict = {}
_google_error: str | None = None

# How long a Google Contacts snapshot is trusted. Local edits are caught for
# free by file mtime; Google offers nothing to watch, so it needs a clock.
GOOGLE_TTL_SECONDS = 900


def _addressbook_fingerprint():
    """Cheap value that changes whenever a local address book file is written."""
    stamps = []
    for path in sorted(glob.glob(ADDRESSBOOK_GLOB, recursive=True)):
        for suffix in ("", "-wal"):
            try:
                stat = os.stat(path + suffix)
            except OSError:
                continue
            stamps.append((path + suffix, stat.st_mtime, stat.st_size))
    return tuple(stamps)


def _contacts():
    """handle -> name, merged from the local address book and Google Contacts.

    Both are needed, not either: they overlap only partially. Measured here, 16
    of 27 local handles were absent from Google, and 183 Google handles were
    absent locally — so either source alone silently loses names.

    Local wins a conflict, because that is the name Messages.app itself shows on
    this Mac and disagreeing with the app on screen is worse than either name.

    The cache expires, because these servers outlive the data. This one held a
    snapshot for 29 hours across both a credential revocation and a contact
    rename, still answering with a name the address books no longer contained —
    and a stale name is worse than a slow one, since nothing about the answer
    looks wrong. A local edit changes an address book file, so mtime catches it
    immediately and for free; Google exposes nothing to watch, so it gets a TTL.
    """
    global _contacts_cache, _contacts_fingerprint
    fingerprint = (_addressbook_fingerprint(), int(time.monotonic() // GOOGLE_TTL_SECONDS))
    if _contacts_cache is not None and fingerprint == _contacts_fingerprint:
        return _contacts_cache
    _contacts_fingerprint = fingerprint

    local = _addressbook_contacts()
    google = _google_contacts()

    merged = dict(google)
    merged.update(local)

    _sources_cache.clear()
    _card_handles.clear()
    for key, (_name, raw) in merged.items():
        _sources_cache[key] = "local" if key in local else "google"
        _card_handles[key] = raw

    _contacts_cache = {key: name for key, (name, _raw) in merged.items()}
    return _contacts_cache


def _display(handle):
    """Contact name for a handle, falling back to the handle itself."""
    if not handle:
        return "me"
    return _contacts().get(_normalise(handle), handle)


def _osascript(script, *args):
    result = subprocess.run(
        ["osascript", "-e", script, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript failed")
    return result.stdout.strip()


def _require_handle(value: str) -> str:
    """`value` if it is a phone number or email, else ValueError.

    Tools that act on one person take a handle, never a name. Choosing a person
    from a name is a judgment between people, and it belongs to the caller
    reading list_contacts: a matcher here would pick silently, and a substring
    match picks wrongly ("Al" is inside "Sally").
    """
    value = value.strip()
    digits = sum(c.isdigit() for c in value)
    if "@" in value or (digits >= 3 and re.fullmatch(r"\+?[\d\s().-]+", value)):
        return value
    raise ValueError(f"{value!r} is not a phone number or email; find the "
                     "person in list_contacts and pass their handle")


def _one_to_one_chat(handle: str):
    """(chat_id, guid) of the one-to-one conversation with a handle, or None."""
    key = _normalise(handle)
    best = None
    for cid, guid, identifier, last_date in _query(
        """select c.ROWID, c.guid, c.chat_identifier, max(j.message_date)
             from chat c left join chat_message_join j on j.chat_id = c.ROWID
            where c.style = 45
            group by c.ROWID"""
    ):
        if _normalise(identifier) == key and (best is None or (last_date or 0) > best[2]):
            best = (cid, guid, last_date or 0)
    return best[:2] if best else None


def _status(is_sent, is_delivered, error):
    """Outcome of an outgoing message as Messages recorded it.

    `sent` is as far as SMS and RCS ever get here: they carry no delivery
    receipt, so is_delivered stays 0 on most of them. For iMessage, `sent` means
    Apple accepted it and the recipient's devices have not acknowledged yet.
    """
    if error:
        return "failed"
    if is_delivered:
        return "delivered"
    if is_sent:
        return "sent"
    return "pending"


# ---------------------------------------------------------------- read tools


def _source(path: str) -> pathlib.Path:
    """`path` as a file on this Mac, else ValueError."""
    source = pathlib.Path(path).expanduser()
    if not source.is_absolute():
        raise ValueError(f"{path!r} is not an absolute path on this Mac")
    if not source.is_file():
        raise ValueError(f"{path!r} is not a file on this Mac")
    return source


def _stage(source: pathlib.Path) -> pathlib.Path:
    """A path Messages can send `source` from: the file itself when it is already
    inside ATTACHMENTS, else a copy in OUTBOX keeping its name, which the
    recipient sees."""
    if source.resolve().is_relative_to(ATTACHMENTS.resolve()):
        return source
    staged = OUTBOX / uuid.uuid4().hex / source.name
    staged.parent.mkdir(parents=True)
    shutil.copy2(source, staged)
    return staged


def _await_outcome(since, chat_id=None, handle=None, text=None, filename=None,
                   claimed=frozenset()):
    """The message row one part of a send just created, once it is sent,
    delivered or failed.

    The AppleScript send returns as soon as Messages accepts the request and says
    nothing about whether the message went anywhere. An iMessage send to a number
    that is not on iMessage returns cleanly and then fails with error 22, so the
    outcome is read back from chat.db rather than assumed. Returns None if no
    matching row appeared within SEND_WAIT_SECONDS.

    A part is `text`, matched on its body, or a file, matched on the attachment's
    name. `claimed` holds rows already matched to earlier parts of the same send.
    A file Messages could not read leaves a placeholder row with no attachment,
    marked delivered; that is reported as failed rather than believed.
    """
    since_apple = int((since - APPLE_EPOCH_OFFSET) * 1e9)
    key = _normalise(handle) if handle else None
    deadline = time.monotonic() + SEND_WAIT_SECONDS
    while True:
        found = orphan = None
        for (rid, body, blob, service, is_sent, is_delivered, error,
             cid, identifier, style, transfer_name, transfer_state) in _query(
            """select m.ROWID, m.text, m.attributedBody, m.service, m.is_sent,
                      m.is_delivered, m.error, c.ROWID, c.chat_identifier, c.style,
                      a.transfer_name, a.transfer_state
                 from message m
                 join chat_message_join j on j.message_id = m.ROWID
                 join chat c on c.ROWID = j.chat_id
                 left join message_attachment_join aj on aj.message_id = m.ROWID
                 left join attachment a on a.ROWID = aj.attachment_id
                where m.is_from_me = 1 and m.date >= ?
                order by m.date desc""",
            (since_apple,),
        ):
            if rid in claimed:
                continue
            if chat_id is not None and cid != chat_id:
                continue
            if key is not None and (style != 45 or _normalise(identifier) != key):
                continue
            if filename is not None:
                if transfer_name is None and _body(body, blob) == PLACEHOLDER:
                    orphan = rid
                if transfer_name != filename:
                    continue
            elif transfer_name is not None or (_body(body, blob) or "").strip() != text.strip():
                continue
            status = _status(is_sent, is_delivered, error)
            if filename is not None and not error and transfer_state != TRANSFER_FINISHED:
                status = "pending"
            found = {"message_id": rid, "chat_id": cid, "service": service,
                     "status": status}
            if error:
                found["error"] = error
            break
        if (found and found["status"] != "pending") or time.monotonic() >= deadline:
            # Only once nothing better turned up: the attachment row can land a
            # moment after its message row.
            if found is None and orphan is not None:
                return {"message_id": orphan, "status": "failed",
                        "detail": "Messages recorded the message with no file attached"}
            return found
        time.sleep(SEND_POLL_SECONDS)


def list_chats(limit: int = 20, include_groups: bool = True) -> list[dict]:
    """List recent conversations, most recently active first.

    Args:
        limit: how many conversations to return.
        include_groups: include group chats as well as one-to-one threads.
    """
    group_filter = "" if include_groups else "and (c.style is null or c.style != 43)"
    rows = _query(
        f"""
        select c.ROWID, c.chat_identifier, c.display_name, c.style, c.service_name,
               max(m.date) as last_date,
               (select m2.ROWID from message m2
                  join chat_message_join j2 on j2.message_id = m2.ROWID
                 where j2.chat_id = c.ROWID order by m2.date desc limit 1)
        from chat c
        join chat_message_join j on j.chat_id = c.ROWID
        join message m on m.ROWID = j.message_id
        where 1=1 {group_filter}
        group by c.ROWID
        order by last_date desc
        limit ?
        """,
        (limit,),
    )

    # One lookup for every chat's last message, rather than a correlated subquery
    # per column. Without the sender a group's last line is unattributable.
    last_ids = [r[6] for r in rows if r[6]]
    last = {}
    if last_ids:
        placeholders = ",".join("?" * len(last_ids))
        for mid, text, blob, is_from_me, handle, assoc in _query(
            f"""select m.ROWID, m.text, m.attributedBody, m.is_from_me, h.id,
                       m.associated_message_type
                  from message m left join handle h on h.ROWID = m.handle_id
                 where m.ROWID in ({placeholders})""", last_ids):
            kind, removed = _reaction(assoc or 0)
            last[mid] = {
                "text": _body(text, blob),
                "from": "me" if is_from_me else _display(handle),
                "reaction": kind,
                "reaction_removed": removed,
            }

    chats = []
    for cid, identifier, display_name, style, service, last_date, last_id in rows:
        tail = last.get(last_id, {})
        text, blob = tail.get("text"), None
        participants = [
            _display(h[0])
            for h in _query(
                """select h.id from handle h
                     join chat_handle_join j on j.handle_id = h.ROWID
                    where j.chat_id = ?""",
                (cid,),
            )
        ]
        chats.append(
            {
                "chat_id": cid,
                "name": display_name or ", ".join(participants) or identifier,
                "identifier": identifier,
                "is_group": style == 43,
                "service": service,
                "participants": participants,
                "last_message_at": _apple_time(last_date),
                "last_message": text,
                "last_message_from": tail.get("from"),
                "last_message_reaction": tail.get("reaction"),
            }
        )
    return chats


def get_thread(chat_id: int | None = None, contact: str | None = None,
               limit: int = 50) -> list[dict]:
    """Read messages from one conversation, oldest to newest.

    Messages from the user carry `status` (delivered, sent, failed, pending) and,
    when failed, Messages' `error` code.

    Args:
        chat_id: conversation id from list_chats or list_contacts.
        contact: alternatively a phone number or email; names are refused, so
            resolve a person through list_contacts first.
        limit: how many of the most recent messages to return.
    """
    if chat_id is None and contact is None:
        raise ValueError("pass either chat_id or contact")

    if chat_id is None:
        chat = _one_to_one_chat(_require_handle(contact))
        if chat is None:
            return []
        chat_id = chat[0]

    rows = _query(
        """
        select m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
               h.id, m.service, m.cache_has_attachments,
               m.guid, m.associated_message_type, m.associated_message_guid,
               m.is_sent, m.is_delivered, m.error
        from message m
        join chat_message_join j on j.message_id = m.ROWID
        left join handle h on h.ROWID = m.handle_id
        where j.chat_id = ?
        order by m.date desc
        limit ?
        """,
        (chat_id, limit),
    )

    by_guid = {r[8]: r[0] for r in rows if r[8]}

    # cache_has_attachments is a bare boolean, so nothing built on it can open the
    # file. Carry the guid, mime type and path so a caller can.
    shots = attachments(chat_id)

    messages = []
    for (rid, text, blob, date, is_from_me, handle, service, has_attachments,
         _guid, assoc_type, assoc_guid, is_sent, is_delivered, error) in rows:
        entry = {
            "id": rid,
            "from": "me" if is_from_me else _display(handle),
            "is_from_me": bool(is_from_me),
            "text": _body(text, blob),
            "at": _apple_time(date),
            "service": service,
            "has_attachments": bool(has_attachments),
            "attachments": shots.get(rid, []),
        }
        if is_from_me:
            entry["status"] = _status(is_sent, is_delivered, error)
            if error:
                entry["error"] = error
        kind, removed = _reaction(assoc_type or 0)
        if kind:
            target = _assoc_guid(assoc_guid)
            entry["reaction"] = kind
            entry["reaction_removed"] = removed
            # None when the reacted-to message falls outside this page.
            entry["reacts_to"] = by_guid.get(target)
        messages.append(entry)
    return list(reversed(messages))


def search_messages(query: str, limit: int = 30, contact: str | None = None) -> list[dict]:
    """Search message text across all conversations, newest first.

    Covers messages whose content is only in the encoded blob as well as those
    with a plain text column, so nothing is silently missed. Matching is
    case-insensitive and happens in Python, walking newest-first and stopping at
    the limit; a full no-match scan of the whole database costs well under a
    second.

    Args:
        query: substring to look for.
        limit: how many matches to return.
        contact: optionally restrict to one phone number or email; names are
            refused, so resolve a person through list_contacts first.
    """
    sql = """
        select m.ROWID, m.text, m.attributedBody, m.date, m.is_from_me,
               h.id, m.service, j.chat_id
        from message m
        left join handle h on h.ROWID = m.handle_id
        left join chat_message_join j on j.message_id = m.ROWID
    """
    params: list = []

    if contact:
        chat = _one_to_one_chat(_require_handle(contact))
        if chat is None:
            return []
        sql += " where j.chat_id = ?"
        params.append(chat[0])

    sql += " order by m.date desc"

    needle = query.lower()
    matches = []
    for rid, text, blob, date, is_from_me, handle, service, cid in _query(sql, params):
        body = _body(text, blob)
        if not body or needle not in body.lower():
            continue
        matches.append(
            {
                "id": rid,
                "chat_id": cid,
                "from": "me" if is_from_me else _display(handle),
                "text": body,
                "at": _apple_time(date),
                "service": service,
            }
        )
        if len(matches) >= limit:
            break
    return matches


def list_contacts() -> dict:
    """Every contact's phone numbers and emails, most recently messaged first.

    This returns the whole address book rather than searching it, on purpose. A
    name to look up usually arrives through speech-to-text, so it is a guess at a
    spelling ("Mia" for "Maya"), and there may be several people it could mean.
    Choose the person by reading the list: the name that fits, weighed by who the
    user actually talks to. Ask the user when that still leaves more than one.

    Each row is one handle, so a person with a phone and an email appears twice.
    A row with `chat_id` has a one-to-one conversation: `last_message_at` and
    `messages` describe it, and `service` is the service of its latest message
    (iMessage, SMS or RCS). A row without one has never been messaged one-to-one.

    Send by passing the chosen row's `handle` to send_message as `to`.
    """
    # SQLite takes bare columns from the row that produced the single max(), so
    # m.service here is the service of each conversation's latest message.
    threads = {}
    for cid, identifier, count, last_date, service in _query(
        """select c.ROWID, c.chat_identifier, count(*), max(j.message_date), m.service
             from chat c
             join chat_message_join j on j.chat_id = c.ROWID
             join message m on m.ROWID = j.message_id
            where c.style = 45
            group by c.ROWID"""
    ):
        key = _normalise(identifier)
        if key not in threads or last_date > threads[key][3]:
            threads[key] = (cid, identifier, count, last_date, service)

    ranked = []
    for key, name in _contacts().items():
        row = {"name": name, "handle": _card_handles[key],
               "source": _sources_cache[key]}
        last_date = 0
        thread = threads.get(key)
        if thread:
            cid, identifier, count, last_date, service = thread
            # The conversation's own identifier, so send_message finds it again.
            row.update(handle=identifier, chat_id=cid,
                       last_message_at=_apple_time(last_date),
                       messages=count, service=service)
        ranked.append((-last_date, name.lower(), row))
    ranked.sort(key=lambda r: r[:2])

    result = {"contacts": [row for _, _, row in ranked]}
    if _google_error:
        # Names are missing rather than wrong, so a caller that sees only local
        # results would otherwise conclude the person has no contact card.
        result["google_contacts"] = {"status": "unavailable",
                                     "detail": _google_error,
                                     "fix": "re-run gmail-mcp/authorize.py"}
    return result


# ---------------------------------------------------------------- send tool


# Both scripts end with the payload and its kind, "text" or "file". The payload
# is argv, never script source, so message text cannot break out into
# AppleScript; a file payload is a path AppleScript itself turns into a file.
SEND_TO_CHAT = """
    on run argv
      set targetChat to item 1 of argv
      set payload to item 2 of argv
      tell application "Messages"
        if item 3 of argv is "file" then
          send (POSIX file payload) to chat id targetChat
        else
          send payload to chat id targetChat
        end if
      end tell
    end run
"""

SEND_TO_HANDLE = """
    on run argv
      set targetHandle to item 1 of argv
      set serviceName to item 2 of argv
      set payload to item 3 of argv
      tell application "Messages"
        if serviceName is "SMS" then
          set targetService to 1st account whose service type = SMS
        else
          set targetService to 1st account whose service type = iMessage
        end if
        if item 4 of argv is "file" then
          send (POSIX file payload) to participant targetHandle of targetService
        else
          send payload to participant targetHandle of targetService
        end if
      end tell
    end run
"""

# Messages attaches only files it can read, and that is not most of the disk. A
# path in /tmp or ~/repos is not refused: the send returns cleanly and Messages
# records an empty placeholder bubble with no file, marked delivered. Its own
# Attachments folder always works, so a file outside it is copied into OUTBOX
# before sending, and one already inside it (an upload, or an attachment someone
# sent) is sent as it is.
#
# The copy is not temporary. Messages sends a file inside that folder in place,
# and the attachment row points at that path, so the staged copy is the Mac's
# only record of what was sent. Deleting it breaks the attachment in Messages'
# history.
ATTACHMENTS = pathlib.Path.home() / "Library/Messages/Attachments"
OUTBOX = ATTACHMENTS / "messages-mcp"

# How long send_message waits for Messages to record an outcome before reporting
# whatever state the message is in. This bounds the report, not delivery: the
# wait ends at the first of sent, delivered or failed, normally within a couple
# of seconds. Waiting for delivery itself is not possible: over the last month
# iMessage delivery averaged hours (a recipient's phone off overnight), and SMS
# never reports delivery at all.
SEND_WAIT_SECONDS = 20
SEND_POLL_SECONDS = 0.25

# A file's message row can read sent before its upload has finished; the
# attachment's own transfer_state reaching 5 is what says the file went.
TRANSFER_FINISHED = 5
PLACEHOLDER = "￼"

# Worst first: a send of several parts reports the weakest of them.
STATUS_RANK = ("failed", "unconfirmed", "pending", "sent", "delivered")


def send_message(text: str | None = None, to: str | None = None,
                 chat_id: int | None = None, service: str | None = None,
                 files: list[str] | None = None) -> dict:
    """Send a message, files, or both from the user's own identity via Messages.app.

    Never pass a name. Resolve the person first, with list_contacts for one
    person or list_chats for a group, and pass exactly what it returned.

    `to` is a phone number or email. When a one-to-one conversation with it
    already exists, the message goes into that conversation and Messages picks
    iMessage or SMS itself, exactly as when the user types in that thread. With
    no conversation yet, a phone number needs `service` ("iMessage" or "SMS")
    because nothing here knows which it is on; an email can only be iMessage.
    Passing `service` with an existing conversation forces it, e.g. to retry as
    SMS after an iMessage send failed.

    `chat_id` (from list_chats) sends into that exact conversation, and is the
    only way to reach a group: a group has no single handle to address.

    `files` are absolute paths on the Mac this server runs on, including when it
    is called over HTTP from another machine. To send a file that exists only on
    the caller's machine, upload it first: on the Pi,
    `~/repos/messages-mcp/bin/upload <file>` prints the Mac path to pass here.
    Each goes as its own message, in order, before `text`. Every path is checked
    before anything is sent.

    The result's `status` is the weakest of its `parts`, each of which carries
    its own: `delivered`; `sent` (accepted but not yet delivered, which is final
    for SMS since it has no delivery receipt); `failed`, with Messages' `error`
    code, or with `detail` when a file's message arrived with no file attached;
    `pending` if still unsent, or a file still uploading, when the tool stopped
    waiting; or `unconfirmed` if no sent message appeared. get_thread shows the
    same status later.

    Requires the Mac awake and signed in. SMS is relayed through the iPhone, so
    that leg also needs the phone on.

    Args:
        text: message body, sent verbatim. Optional when files are given.
        to: phone number or email for a one-to-one message.
        chat_id: conversation id from list_chats; required for group chats.
        service: "iMessage" or "SMS"; see above for when it is needed.
        files: absolute paths on this Mac to send as attachments.
    """
    if (to is None) == (chat_id is None):
        raise ValueError("pass exactly one of to or chat_id")
    if service not in (None, "iMessage", "SMS"):
        raise ValueError("service must be 'iMessage' or 'SMS'")
    if not text and not files:
        raise ValueError("pass text, files, or both")
    sources = [(path, _source(path)) for path in files or []]

    if chat_id is not None:
        if service is not None:
            raise ValueError("service applies only to `to`; a conversation "
                             "already has its own service")
        row = _query_one(
            "select guid, style, display_name, chat_identifier from chat where ROWID = ?",
            (chat_id,),
        )
        if row is None:
            raise ValueError(f"no chat with id {chat_id}")
        guid, style, display_name, identifier = row
        send = functools.partial(_osascript, SEND_TO_CHAT, guid)
        target = {"chat_id": chat_id, "chat": display_name or identifier,
                  "is_group": style == 43}
        where = {"chat_id": chat_id}
    else:
        handle = _require_handle(to)
        existing = _one_to_one_chat(handle)
        if existing and service is None:
            send = functools.partial(_osascript, SEND_TO_CHAT, existing[1])
        else:
            if service is None:
                if "@" not in handle:
                    raise ValueError(f"no conversation with {handle} yet, so its "
                                     "service is unknown; pass service='iMessage' or 'SMS'")
                service = "iMessage"
            send = functools.partial(_osascript, SEND_TO_HANDLE, handle, service)
        target = {"to": handle}
        where = {"handle": handle}

    # Each part is awaited before the next is sent, which keeps them in order and
    # gives every one its own outcome.
    parts, claimed = [], set()

    def deliver(label, payload, kind, **match):
        started = time.time()
        send(payload, kind)
        outcome = _await_outcome(started, claimed=claimed, **where, **match)
        if outcome:
            claimed.add(outcome["message_id"])
        parts.append({**label, **(outcome or {"status": "unconfirmed"})})

    for path, source in sources:
        staged = _stage(source)
        deliver({"file": path}, str(staged), "file", filename=staged.name)
    if text:
        deliver({"text": text}, text, "text", text=text)

    status = min((part["status"] for part in parts), key=STATUS_RANK.index)
    return {**target, "status": status, "parts": parts}


if __name__ == "__main__":
    mcp.run()


# ---------------------------------------------------------------- public surface
#
# The MCP server reaches these through its tools, but an agent on this Mac reads
# chat.db in its own shape - every message oldest-first with rowids, say - and
# going through an MCP client to do that would add a transport and a JSON round
# trip to a local file read. These are the primitives it is meant to use.

query = _query                 # rows for a SQL statement, connection closed
query_one = _query_one         # first row or None
body = _body                   # message text, decoding attributedBody when needed
display = _display             # handle -> name, merged from both address books


def attachments(chat_rowid):
    """Every attachment in one chat, keyed by message rowid.

    `cache_has_attachments` on a message is a bare boolean, so nothing built on it
    can find the file. These are the fields that let a caller open one: the guid to
    cache a result against, the mime type to decide whether it is readable, and the
    path on disk, which needs expanduser before use.
    """
    out = {}
    for mid, guid, mime, name in _query(
        """select maj.message_id, a.guid, a.mime_type, a.filename
             from message_attachment_join maj
             join attachment a on a.ROWID = maj.attachment_id
             join chat_message_join j on j.message_id = maj.message_id
            where j.chat_id = ?
            order by maj.message_id, a.ROWID""",
        (chat_rowid,),
    ):
        out.setdefault(mid, []).append(
            {"guid": guid, "mime_type": mime or "", "filename": name or ""})
    return out
