# chatdb

Reads and writes macOS Messages by going straight at `~/Library/Messages/chat.db`.
Everything on this Mac that touches Messages goes through here: the `messages-mcp`
server wraps it in MCP tools, and JaredBot uses it directly.

## Why it is a package and not part of the MCP server

An agent running on this Mac needs the thread in its own shape — every message
oldest-first with rowids and raw Apple timestamps — and reaching that through an
MCP client would put a transport and a JSON round trip in front of a local file
read. So the primitives are public, and the MCP server is one of two consumers
rather than the owner:

| | |
|---|---|
| `query(sql, params)` | rows, connection closed |
| `query_one(sql, params)` | first row or `None` |
| `body(text, blob)` | message text, decoding `attributedBody` when needed |
| `display(handle)` | handle → name, merged from both address books |
| `attachments(chat_rowid)` | `{message_rowid: [{guid, mime_type, filename}]}` |
| `list_chats` `get_thread` `search_messages` `list_contacts` `send_message` | what the MCP tools expose |

## Install

    pip install -e ~/repos/chatdb

Needs `pyobjc-framework-Cocoa` for the decode. Consumers share the venv at
`~/repos/messages-mcp/.venv`, which has it.

## Full Disk Access

Without it `chat.db` will not open and staging a file into Messages' Attachments
folder fails with `PermissionError`. The grant belongs to whichever binary runs
the code, and for the launchd jobs that is **`~/repos/messages-mcp/bin/fda-launch`**,
which stays there rather than moving in here: the grant is bound to that binary's
path and signature, so relocating or rebuilding it voids the grant with no error
anywhere — reads simply start failing. Both plists point at it.

## Two things about chat.db that shape this code

**Message text is often not in the `text` column.** About a third of rows have
`text` NULL and the content in `attributedBody`, an `NSAttributedString` in the old
typedstream format. `body()` decodes it through Apple's own `NSUnarchiver`, so a
format change is Apple's problem rather than a parser rewrite. Reading only `text`
silently loses a third of every conversation, including whole threads.

That decode runs inside an explicit `objc.autorelease_pool()`, and the wrapper is
load-bearing. `NSData` and the unarchived string are autoreleased, and a process
with no Cocoa run loop never drains the pool, so without it every message ever
decoded stays resident — about 1.3 KB each, gigabytes in a long-running watcher.

**Senders are handles, not names.** Names merge the Mac's local AddressBook with
Google Contacts, matched on the last ten digits. Neither source alone covers the
handles here, and a name missing from one is not missing from both.

## Attachments

`message.cache_has_attachments` is a bare boolean, so nothing built on it can open
the file. `attachments()` carries the `guid` to cache a result against, the
`mime_type` to decide whether it is readable, and `filename`, the path on disk —
which needs `expanduser` and may be empty for an attachment that was never
downloaded. `get_thread` returns the same per message.
