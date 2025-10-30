"""
MCP Server for iCloud Email and Calendar
=======================================

This module implements a fully-featured Model Context Protocol (MCP) server
that exposes both iCloud Mail (IMAP/SMTP) and iCloud Calendar (CalDAV) as
tools.  The server uses the `fastmcp` framework to handle the protocol
machinery.  Each function decorated with `@mcp.tool()` becomes a
callable tool and is automatically registered with the MCP runtime.

**Prerequisites**

Running this server requires a few third-party packages.  These packages
are not bundled with this repository, so be sure to install them in your
environment:

* `fastmcp` - simplifies building MCP servers and clients.
* `caldav` - a CalDAV client used for calendar operations.
* `python-dotenv` - loads environment variables from a `.env` file.

You can install these dependencies with pip:

```bash
pip install fastmcp caldav python-dotenv
```

You must also generate an **app-specific password** from Apple and set
environment variables in a `.env` file alongside this script.  See
```.env.example``` for a template.

**Functionality**

The server provides the following categories of tools:

* **Mailbox management:** list available mailboxes, search within a
  mailbox, fetch message summaries, fetch complete messages with bodies
  and attachment metadata, download individual attachments, move
  messages between folders, delete and archive messages, create drafts
  and send new mail.  Flags such as read/unread or starred can also be
  toggled via the `flag_message` tool.

* **Calendar management:** list calendars, list events within a time
  range (including recurring expansion), create new events, update
  existing events, delete events, perform free-text search across
  events, and fetch raw iCalendar (ICS) blobs for a given event UID.

All tools return their results as structured content (JSON objects)
under the ``structuredContent`` field of the MCP tool result.  For
backwards compatibility with simple clients, the JSON is also
serialized into a single text block in the ``content`` field.

**Security considerations**

Keep this server private.  Your iCloud app-specific password grants
full read/write access to your email and calendar.  Exposing the
server without authentication is equivalent to publishing your
credentials.  When deploying, place the server behind an HTTPS
reverse proxy and enable authentication (for example, HTTP basic
auth, an OAuth proxy, or Cloudflare Access) to protect it from
unauthorized access.

This server is designed for personal automation and AI agents.  It is
not a general purpose mail gateway and should not be used for high
volume or multi-tenant scenarios without substantial hardening.
"""

from __future__ import annotations

import base64
import datetime as dt
import email
import imaplib
import json
import logging
import os
import re
import smtplib
import time
from html.parser import HTMLParser
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from starlette.requests import Request
from starlette.responses import PlainTextResponse

try:
    # CalDAV support for calendar functions.  If this import fails you
    # likely forgot to install the `caldav` package.  Calendar tools
    # will not work without it.
    from caldav.davclient import DAVClient  # type: ignore
except Exception:
    DAVClient = None  # type: ignore

try:  # Optional but strongly recommended for calendar manipulation helpers
    from icalendar import Alarm as ICalendarAlarm  # type: ignore
    from icalendar import Calendar as ICalendarCalendar  # type: ignore
    from icalendar import Event as ICalendarEvent  # type: ignore
except Exception:  # pragma: no cover - exercised when dependency is missing
    ICalendarAlarm = None  # type: ignore
    ICalendarCalendar = None  # type: ignore
    ICalendarEvent = None  # type: ignore


# ---------------------------------------------------------------------------
#  Configuration
#
# Environment variables are loaded from a .env file located next to this
# script.  Required variables include your Apple ID (email form) and an
# app-specific password.  Optional variables allow you to override
# server host/port and mailbox names.
# ---------------------------------------------------------------------------

# Always load .env relative to this file, regardless of the current
# working directory.  This makes development easier when running via
# `python server.py` from different locations.
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)


def _require_env(name: str, default: Optional[str] = None) -> str:
    """Helper to fetch a required environment variable."""
    value = os.environ.get(name, default)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


# Required iCloud credentials
APPLE_ID: str = _require_env("APPLE_ID")
ICLOUD_APP_PASSWORD: str = _require_env("ICLOUD_APP_PASSWORD")

# Mail settings with sensible defaults.  Override these if you use
# custom ports or hostnames.
IMAP_SERVER: str = os.environ.get("IMAP_SERVER", "imap.mail.me.com").strip()
IMAP_PORT: int = int(os.environ.get("IMAP_PORT", "993"))
SMTP_SERVER: str = os.environ.get("SMTP_SERVER", "smtp.mail.me.com").strip()
SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USE_SSL: bool = os.environ.get("SMTP_USE_SSL", "").lower() in {"1", "true", "yes"}

# CalDAV settings
CALDAV_URL: str = os.environ.get("CALDAV_URL", "https://caldav.icloud.com").strip()
DEFAULT_TZID: str = os.environ.get("TZID", "America/New_York").strip()

# Default mailboxes for common operations.  You can override these
# names if your account uses localized or custom folder names.
ARCHIVE_MAILBOX: str = os.environ.get("ARCHIVE_MAILBOX", "Archive").strip()
TRASH_MAILBOX: str = os.environ.get("TRASH_MAILBOX", "Trash").strip()
DRAFTS_MAILBOX: str = os.environ.get("DRAFTS_MAILBOX", "Drafts").strip()
SENT_MAILBOX: str = os.environ.get("SENT_MAILBOX", "Sent").strip()

# Server address configuration.  These control where the HTTP server
# listens.  Note: when exposing to the public Internet you should
# reverse proxy behind HTTPS and enable authentication.
SERVER_HOST: str = os.environ.get("HOST", "127.0.0.1").strip()
SERVER_PORT: int = int(os.environ.get("PORT", "8000"))

# Calendar search tuning allows balancing coverage vs. latency.  These
# settings can be overridden via environment variables if broader time
# ranges or more results are required.
SEARCH_SCAN_DAYS_DEFAULT: int = max(1, int(os.environ.get("SCAN_DAYS", str(365))))
SEARCH_CHUNK_DAYS_DEFAULT: int = max(1, int(os.environ.get("SCAN_CHUNK_DAYS", "90")))
SEARCH_MAX_RESULTS_DEFAULT: int = max(1, int(os.environ.get("MAX_EVENT_RESULTS", "200")))


# Logging configuration.  INFO level is noisy enough to surface errors
# while not spamming every IMAP/SMTP operation.  Adjust as needed.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("icloud-mcp")


# ---------------------------------------------------------------------------
#  MCP Server
#
# We create a FastMCP instance and define a health check endpoint.  Tools
# for both email and calendar are registered below using decorators.
# ---------------------------------------------------------------------------

mcp = FastMCP("icloud-mail-calendar", instructions=(
    "This server exposes iCloud email and calendar functionality via the "
    "Model Context Protocol.  Use the provided tools to search, fetch, "
    "manage, create and update mail and events.  All dates should be "
    "supplied in ISO 8601 format (YYYY-MM-DDTHH:MM:SS, optionally with "
    "timezone offsets).  For calendar operations, recurring events are "
    "automatically expanded when requested."
))


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    """Simple health check for infrastructure monitoring."""
    return PlainTextResponse("OK")


# ---------------------------------------------------------------------------
#  Helper functions for Mail operations
# ---------------------------------------------------------------------------

def _open_imap() -> imaplib.IMAP4_SSL:
    """Create and authenticate an IMAP4_SSL connection."""
    try:
        conn = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        conn.login(APPLE_ID, ICLOUD_APP_PASSWORD)
        return conn
    except Exception as exc:
        log.error("IMAP connection error: %s", exc)
        raise


def _open_smtp() -> smtplib.SMTP:
    """Create and authenticate an SMTP connection (TLS or SSL)."""
    try:
        if SMTP_USE_SSL or SMTP_PORT == 465:
            smtp = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        else:
            smtp = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            # Only start TLS on port 587 by default
            smtp.starttls()
        smtp.login(APPLE_ID, ICLOUD_APP_PASSWORD)
        return smtp
    except Exception as exc:
        log.error("SMTP connection error: %s", exc)
        raise


def _decode_header(value: Optional[str]) -> str:
    """Decode an RFC 2047 encoded header into Unicode."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _parse_imap_list_line(line: bytes) -> Optional[Dict[str, Any]]:
    """
    Parse a single result line from the IMAP LIST command into a
    dictionary with flags, delimiter and mailbox name.
    """
    try:
        text = line.decode()
        # Format: (<flags>) "<delimiter>" <name>
        m = re.match(r"\((?P<flags>[^\)]*)\) \"(?P<delim>[^\"]*)\" (?P<name>.*)", text)
        if not m:
            return None
        flags_raw = m.group('flags').strip()
        flags = flags_raw.split() if flags_raw else []
        delimiter = m.group('delim')
        name = m.group('name').strip()
        # strip surrounding quotes from the mailbox name
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        return {"name": name, "delimiter": delimiter, "flags": flags}
    except Exception:
        return None


def _parse_message_flags(resp: bytes) -> List[str]:
    """
    Extract the list of flags from an IMAP FETCH response prefix.
    The response prefix looks like: b'1 (FLAGS (\\Seen \\Flagged) ...'
    """
    try:
        text = resp.decode()
        m = re.search(r"FLAGS \((.*?)\)", text)
        if not m:
            return []
        flags = m.group(1).split()
        return flags
    except Exception:
        return []


class _HTMLTextExtractor(HTMLParser):
    """Simple HTML to plain text converter for fallback bodies."""

    _BREAK_TAGS = {"br", "p", "div", "section", "article", "li", "tr", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:  # type: ignore[override]
        if tag in {"br", "hr"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag in self._BREAK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if data:
            self._chunks.append(data)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        # Normalize whitespace while preserving intentional breaks
        normalized = re.sub(r"\r\n?", "\n", raw)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.get_text()


def _empty_tool_result() -> ToolResult:
    return _tool_result({})


def _tool_result(payload: Dict[str, Any], *, text: Optional[str] = None) -> ToolResult:
    """Create a ToolResult that keeps both summary text and JSON detail."""
    blocks: List[TextContent] = []
    if text:
        blocks.append(TextContent(type="text", text=text))
    blocks.append(TextContent(type="text", text=json.dumps(payload, indent=2, sort_keys=True, default=str)))
    return ToolResult(content=blocks, structured_content=payload)


ReminderSpec = Dict[str, Any]
NormalizedReminderSpec = Dict[str, Any]


def _format_duration_for_ics(delta: dt.timedelta) -> str:
    """Convert a timedelta into an ICS duration string (e.g. -PT15M)."""
    total_seconds = int(delta.total_seconds())
    sign = '-' if total_seconds < 0 else ''
    total_seconds = abs(total_seconds)
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days}D")
    time_parts: List[str] = []
    if hours:
        time_parts.append(f"{hours}H")
    if minutes:
        time_parts.append(f"{minutes}M")
    if seconds or (not time_parts and not parts):
        time_parts.append(f"{seconds}S")
    if time_parts:
        parts.append('T' + ''.join(time_parts))
    if not parts:
        parts.append('T0S')
    return f"{sign}P{''.join(parts)}"


def _normalize_reminder_spec(reminder: ReminderSpec) -> Optional[NormalizedReminderSpec]:
    """Validate and normalise reminder specifications provided by clients."""
    if not isinstance(reminder, dict):
        return None
    action = str(reminder.get('action', 'DISPLAY') or 'DISPLAY').strip().upper()
    if action not in {'DISPLAY', 'EMAIL'}:
        action = 'DISPLAY'
    description = str(reminder.get('description') or 'Reminder')

    def _to_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    trigger_raw = reminder.get('trigger')
    absolute_raw = reminder.get('absolute') or reminder.get('at')
    minutes_before_start = reminder.get('minutes_before_start')
    minutes_before_end = reminder.get('minutes_before_end')
    seconds_before_start = reminder.get('seconds_before_start')
    seconds_before_end = reminder.get('seconds_before_end')
    related = str(reminder.get('related', 'start')).lower()
    if related not in {'start', 'end'}:
        related = 'start'

    trigger: Union[dt.timedelta, dt.datetime, str]

    if absolute_raw:
        try:
            trigger = _caldav_parse_iso(str(absolute_raw))
        except Exception:
            return None
        related = str(reminder.get('related', 'start')).lower()
        if related not in {'start', 'end'}:
            related = 'start'
    elif trigger_raw:
        trigger = str(trigger_raw)
    else:
        if minutes_before_start is not None:
            minutes_value = _to_float(minutes_before_start)
            if minutes_value is None:
                return None
            trigger = -dt.timedelta(minutes=abs(minutes_value))
            related = 'start'
        elif minutes_before_end is not None:
            minutes_value = _to_float(minutes_before_end)
            if minutes_value is None:
                return None
            trigger = -dt.timedelta(minutes=abs(minutes_value))
            related = 'end'
        elif seconds_before_start is not None:
            seconds_value = _to_float(seconds_before_start)
            if seconds_value is None:
                return None
            trigger = -dt.timedelta(seconds=abs(seconds_value))
            related = 'start'
        elif seconds_before_end is not None:
            seconds_value = _to_float(seconds_before_end)
            if seconds_value is None:
                return None
            trigger = -dt.timedelta(seconds=abs(seconds_value))
            related = 'end'
        else:
            trigger = -dt.timedelta(minutes=15)
            related = 'start'

    normalised: NormalizedReminderSpec = {
        'action': action,
        'description': description,
        'trigger': trigger,
        'related': related,
    }

    repeat_val = reminder.get('repeat')
    try:
        repeat_int = int(repeat_val) if repeat_val is not None else None
    except (TypeError, ValueError):
        repeat_int = None
    if repeat_int and repeat_int > 0:
        normalised['repeat'] = repeat_int
        duration_val = reminder.get('duration')
        if isinstance(duration_val, dt.timedelta):
            normalised['duration'] = duration_val
        elif isinstance(duration_val, (int, float)):
            normalised['duration'] = dt.timedelta(minutes=float(duration_val))
        elif isinstance(duration_val, str):
            normalised['duration'] = duration_val

    return normalised


def _reminder_spec_to_alarm(spec: NormalizedReminderSpec):
    """Convert a normalised spec into an icalendar Alarm component if possible."""
    if ICalendarAlarm is None:
        return None
    alarm = ICalendarAlarm()
    alarm.add('action', spec['action'])
    alarm.add('description', spec['description'])
    trigger = spec['trigger']
    if isinstance(trigger, dt.timedelta):
        alarm.add('trigger', trigger)
    elif isinstance(trigger, dt.datetime):
        alarm.add('trigger', trigger)
        alarm['TRIGGER'].params['VALUE'] = 'DATE-TIME'
    else:
        alarm.add('trigger', trigger)
    if spec.get('related') == 'end':
        alarm['TRIGGER'].params['RELATED'] = 'END'
    repeat_val = spec.get('repeat')
    if isinstance(repeat_val, int) and repeat_val > 0:
        alarm.add('repeat', repeat_val)
        duration_val = spec.get('duration')
        if isinstance(duration_val, dt.timedelta):
            alarm.add('duration', duration_val)
        elif isinstance(duration_val, (int, float)):
            alarm.add('duration', dt.timedelta(minutes=float(duration_val)))
        elif isinstance(duration_val, str):
            alarm.add('duration', duration_val)
    return alarm


def _reminder_spec_to_ics_lines(spec: NormalizedReminderSpec) -> List[str]:
    """Render a normalised reminder specification into raw ICS VALARM lines."""
    trigger = spec['trigger']
    trigger_params: List[str] = []
    if isinstance(trigger, dt.timedelta):
        trigger_value = _format_duration_for_ics(trigger)
    elif isinstance(trigger, dt.datetime):
        trigger_params.append('VALUE=DATE-TIME')
        trigger_value = trigger.astimezone(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    else:
        trigger_value = str(trigger)
    if spec.get('related') == 'end':
        trigger_params.append('RELATED=END')
    param_segment = ''
    if trigger_params:
        param_segment = ';' + ';'.join(trigger_params)
    lines = [
        'BEGIN:VALARM',
        f"ACTION:{spec['action']}",
        f"DESCRIPTION:{_ics_escape(spec['description'])}",
        f"TRIGGER{param_segment}:{trigger_value}",
    ]
    repeat_val = spec.get('repeat')
    if isinstance(repeat_val, int) and repeat_val > 0:
        lines.append(f"REPEAT:{repeat_val}")
        duration_val = spec.get('duration')
        if isinstance(duration_val, dt.timedelta):
            lines.append(f"DURATION:{_format_duration_for_ics(duration_val)}")
        elif isinstance(duration_val, (int, float)):
            lines.append(f"DURATION:{_format_duration_for_ics(dt.timedelta(minutes=float(duration_val)))}")
        elif isinstance(duration_val, str):
            lines.append(f"DURATION:{duration_val}")
    lines.append('END:VALARM')
    return lines



# ---------------------------------------------------------------------------
#  Mail Tools
#
# All functions in this section are exposed as MCP tools.  They open
# IMAP/SMTP connections on demand, perform the requested operation and
# return their results in a JSON-serializable form.  Connections are
# closed immediately after use.
# ---------------------------------------------------------------------------


@mcp.tool()
def list_mailboxes() -> ToolResult:
    """
    List all available mailboxes in the authenticated user's account.

    Returns a list of objects with the following fields:

    * ``name`` - the mailbox name (string).
    * ``delimiter`` - hierarchy delimiter used by this mailbox (string).
    * ``flags`` - IMAP flags associated with the mailbox (list of strings).
    """
    imap = _open_imap()
    try:
        status, data = imap.list()
        mailboxes: List[Dict[str, Any]] = []
        if status == 'OK' and data:
            for line in data:
                if not isinstance(line, bytes):
                    continue
                entry = _parse_imap_list_line(line)
                if entry:
                    mailboxes.append(entry)
        return _tool_result({"mailboxes": mailboxes}, text=f"{len(mailboxes)} mailbox(es)")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


@mcp.tool()
def list_messages(
    mailbox: str,
    limit: int = 50,
    offset: int = 0
) -> ToolResult:
    """
    List messages within a mailbox, returning basic metadata for each.

    Messages are ordered newest to oldest by UID.  The ``limit`` and
    ``offset`` parameters allow simple pagination.

    Args:
        mailbox: Name of the mailbox to list (e.g., ``"INBOX"``).
        limit: Maximum number of messages to return.
        offset: Number of most recent messages to skip before listing.

    Returns a list of objects with these fields:

    * ``uid`` - unique identifier of the message (string).
    * ``subject`` - decoded subject line (string, may be empty).
    * ``from`` - decoded sender name and address (string).
    * ``to`` - decoded recipient list (string).
    * ``date`` - message date in ISO format (string) or raw header value.
    * ``flags`` - IMAP flags set on the message (list of strings).
    * ``size`` - message size in bytes (integer) if available.
    """
    imap = _open_imap()
    try:
        # Select mailbox in read-only mode to avoid marking unseen messages
        status, _ = imap.select(mailbox, readonly=True)
        if status != 'OK':
            return _tool_result({"messages": []}, text="0 message(s)")
        status, data = imap.uid('SEARCH', None, 'ALL')  # type: ignore[arg-type]
        if status != 'OK' or not data or not data[0]:
            return _tool_result({"messages": []}, text="0 message(s)")
        # data[0] is a space-delimited bytes string of UIDs
        uids = [int(u) for u in data[0].split()]
        uids.sort(reverse=True)
        selected = uids[offset:offset + limit]
        messages: List[Dict[str, Any]] = []
        for uid_int in selected:
            uid_str = str(uid_int)
            status, fetch_data = imap.uid('FETCH', uid_str, '(FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])')
            if status != 'OK' or not fetch_data:
                continue
            # Initialize defaults
            subject = ""
            sender = ""
            to_field = ""
            date_field: Optional[str] = None
            flags: List[str] = []
            size: Optional[int] = None
            # Iterate through the fetch response parts to extract information
            header_bytes = b''
            for part in fetch_data:
                if isinstance(part, tuple):
                    header_bytes = part[1]
                # The response prefix (part[0]) may contain flags and size
                if isinstance(part[0], bytes):
                    prefix = part[0].decode(errors='ignore')
                    # Extract FLAGS
                    m = re.search(r'FLAGS \((.*?)\)', prefix)
                    if m:
                        flags = m.group(1).split()
                    # Extract RFC822.SIZE
                    m2 = re.search(r'RFC822.SIZE (\d+)', prefix)
                    if m2:
                        size = int(m2.group(1))
            # Parse headers if present
            if header_bytes:
                msg = email.message_from_bytes(header_bytes)
                subject = _decode_header(msg.get('Subject'))
                sender = _decode_header(msg.get('From'))
                to_field = _decode_header(msg.get('To'))
                if msg.get('Date'):
                    try:
                        dt_obj = parsedate_to_datetime(msg['Date'])
                        if dt_obj.tzinfo:
                            date_field = dt_obj.isoformat()
                        else:
                            date_field = dt_obj.replace(tzinfo=dt.timezone.utc).isoformat()
                    except Exception:
                        date_field = msg['Date']
            messages.append({
                "uid": uid_str,
                "subject": subject or "",
                "from": sender or "",
                "to": to_field or "",
                "date": date_field,
                "flags": flags,
                "size": size,
            })
        return _tool_result({"messages": messages}, text=f"{len(messages)} message(s)")
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass


@mcp.tool()
def search_messages(
    mailbox: str,
    query: str
) -> ToolResult:
    """
    Search for messages in a mailbox containing a given text fragment.

    This uses the IMAP `TEXT` search term, which matches the entire
    message (headers and body).  The search is case-insensitive and
    returns the list of matching UIDs as strings.

    Args:
        mailbox: Name of the mailbox to search.
        query: A string to search for.  Surrounding quotes are added
            automatically.

    Returns: List of message UIDs (strings) that match the query.
    """
    imap = _open_imap()
    try:
        status, _ = imap.select(mailbox, readonly=True)
        if status != 'OK':
            return _tool_result({"uids": []}, text="0 matches")
        # IMAP SEARCH expects the search terms as separate arguments.  We
        # wrap the query in quotes so that spaces are included in the
        # search term.
        status, data = imap.uid('SEARCH', None, 'TEXT', f'"{query}"')  # type: ignore[arg-type]
        if status != 'OK' or not data or not data[0]:
            return _tool_result({"uids": []}, text="0 matches")
        uids = [uid.decode() if isinstance(uid, bytes) else str(uid) for uid in data[0].split()]
        return _tool_result({"uids": uids}, text=f"{len(uids)} match(es)")
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass


@mcp.tool()
def get_message(
    mailbox: str,
    uid: str
) -> ToolResult:
    """
    Fetch a complete message by UID from the specified mailbox.

    Returns a structured representation of the message including
    headers, body and attachment metadata.  To download the contents of
    an attachment, call the `download_attachment` tool with the
    appropriate parameters.

    Args:
        mailbox: Name of the mailbox containing the message.
        uid: The unique identifier of the message (as returned by
            `list_messages` or `search_messages`).

    Returns: A dictionary with the following keys:

    * ``uid`` - the UID of the message.
    * ``subject`` - decoded subject string.
    * ``from`` - decoded sender string.
    * ``to`` - list of recipients (strings).
    * ``cc`` - list of CC recipients (strings).
    * ``bcc`` - list of BCC recipients (strings).
    * ``date`` - ISO date/time string if parseable, else raw header value.
    * ``message_id`` - Message-ID header value.
    * ``in_reply_to`` - In-Reply-To header value.
    * ``body`` - a dict with ``text`` and ``html`` (strings, empty if absent).
    * ``attachments`` - list of attachment descriptors.  Each item has
        ``attachment_id``, ``filename``, ``content_type`` and ``size``.
    * ``flags`` - list of IMAP flags on the message.
    """
    imap = _open_imap()
    raw_msg: bytes = b''
    flags: List[str] = []
    try:
        status, _ = imap.select(mailbox, readonly=True)
        if status != 'OK':
            return _empty_tool_result()
        status, data = imap.uid('FETCH', uid, '(BODY.PEEK[] FLAGS)')
        if status != 'OK' or not data:
            return _empty_tool_result()
        for part in data:
            if isinstance(part, tuple):
                header_bytes = part[0] if isinstance(part[0], bytes) else None
                payload_bytes = part[1] if isinstance(part[1], (bytes, bytearray)) else None
                if payload_bytes:
                    raw_msg = bytes(payload_bytes)
                if header_bytes:
                    flags.extend(_parse_message_flags(header_bytes))
            elif isinstance(part, bytes):
                flags.extend(_parse_message_flags(part))
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass
    if not raw_msg:
        return _empty_tool_result()
    msg = email.message_from_bytes(raw_msg)
    subject = _decode_header(msg.get('Subject'))
    sender = _decode_header(msg.get('From'))
    to_raw = msg.get('To', '')
    to_list = [s.strip() for s in to_raw.split(',')] if to_raw else []
    cc_raw = msg.get('Cc', '')
    cc_list = [s.strip() for s in cc_raw.split(',')] if cc_raw else []
    bcc_raw = msg.get('Bcc', '')
    bcc_list = [s.strip() for s in bcc_raw.split(',')] if bcc_raw else []
    date_field: Optional[str] = None
    if msg.get('Date'):
        try:
            dt_obj = parsedate_to_datetime(msg['Date'])
            if dt_obj.tzinfo:
                date_field = dt_obj.isoformat()
            else:
                date_field = dt_obj.replace(tzinfo=dt.timezone.utc).isoformat()
        except Exception:
            date_field = msg['Date']
    message_id = msg.get('Message-ID')
    in_reply_to = msg.get('In-Reply-To')
    text_body: str = ""
    html_body: str = ""
    attachments: List[Dict[str, Any]] = []
    att_index = 0
    # Walk over each MIME part and collect body/attachments
    for part in msg.walk():
        content_type = part.get_content_type()
        disp = part.get_content_disposition() or ''
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        payload_bytes = payload if isinstance(payload, (bytes, bytearray)) else None
        if part.get_content_maintype() == 'multipart':
            continue
        if filename or disp.strip().lower().startswith('attachment'):
            payload_size = len(payload_bytes) if payload_bytes else 0
            attachments.append({
                "attachment_id": str(att_index),
                "filename": _decode_header(filename) if filename else f"attachment-{att_index}",
                "content_type": content_type,
                "size": payload_size,
            })
            att_index += 1
        else:
            # Body part
            charset = part.get_content_charset() or 'utf-8'
            text: str = ''
            data = payload_bytes
            if data is None:
                # Some 7bit/8bit sections return str unless explicitly decoded.
                raw_payload = part.get_payload(decode=False)
                if isinstance(raw_payload, str):
                    text = raw_payload
                elif isinstance(raw_payload, bytes):
                    data = raw_payload
            if data is not None and not text:
                try:
                    text = data.decode(charset, errors='replace')
                except Exception:
                    text = data.decode('utf-8', errors='replace')
            if content_type == 'text/plain':
                text_body += text
            elif content_type == 'text/html':
                html_body += text
    if not text_body and html_body:
        text_body = _html_to_text(html_body)

    body_text_clean = text_body.strip() if text_body else ""

    result = {
        "uid": uid,
        "subject": subject,
        "from": sender,
        "to": to_list,
        "cc": cc_list,
        "bcc": bcc_list,
        "date": date_field,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "body": {
            "text": text_body or "",
            "html": html_body or None,
        },
        "attachments": attachments,
        "flags": flags,
    }

    return ToolResult(
        content=[TextContent(type="text", text=body_text_clean or "")],
        structured_content=result,
    )


@mcp.tool()
def download_attachment(
    mailbox: str,
    uid: str,
    attachment_id: str
) -> ToolResult:
    """
    Download a specific attachment from a message.

    Args:
        mailbox: Mailbox where the message resides.
        uid: UID of the message returned by other tools.
        attachment_id: The ``attachment_id`` string from the
            ``attachments`` list in `get_message`.

    Returns: A dictionary containing:

    * ``filename`` - original filename of the attachment.
    * ``content_type`` - MIME type of the attachment.
    * ``data`` - base64-encoded payload of the attachment.
    * ``size`` - size in bytes.
    """
    imap = _open_imap()
    raw_msg: bytes = b''
    try:
        status, _ = imap.select(mailbox, readonly=True)
        if status != 'OK':
            return _tool_result({"attachment": None, "found": False})
        status, data = imap.uid('FETCH', uid, '(BODY.PEEK[])')
        if status != 'OK' or not data:
            return _tool_result({"attachment": None, "found": False})
        for part in data:
            if isinstance(part, tuple):
                payload = part[1]
                if isinstance(payload, (bytes, bytearray)):
                    raw_msg = bytes(payload)
                break
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass
    if not raw_msg:
        return _tool_result({"attachment": None, "found": False})
    msg = email.message_from_bytes(raw_msg)
    idx = 0
    for part in msg.walk():
        disp = part.get_content_disposition() or ''
        filename = part.get_filename()
        if part.get_content_maintype() == 'multipart':
            continue
        if filename or disp.strip().lower().startswith('attachment'):
            if str(idx) == attachment_id:
                payload_raw = part.get_payload(decode=True)
                if isinstance(payload_raw, bytes):
                    payload_bytes = payload_raw
                elif isinstance(payload_raw, bytearray):
                    payload_bytes = bytes(payload_raw)
                else:
                    fallback = part.get_payload(decode=False)
                    if isinstance(fallback, bytes):
                        payload_bytes = fallback
                    elif isinstance(fallback, str):
                        payload_bytes = fallback.encode('utf-8', errors='replace')
                    else:
                        payload_bytes = b''
                encoded = base64.b64encode(payload_bytes).decode('ascii')
                return _tool_result({
                    "attachment": {
                        "filename": _decode_header(filename) if filename else f"attachment-{attachment_id}",
                        "content_type": part.get_content_type(),
                        "data": encoded,
                        "size": len(payload_bytes),
                    },
                    "found": True,
                })
            idx += 1
    return _tool_result({"attachment": None, "found": False})


@mcp.tool()
def send_message(
    to: List[str],
    subject: str,
    body: str,
    html: Optional[str] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    attachments: Optional[List[Dict[str, str]]] = None
) -> ToolResult:
    """
    Send an email message with optional HTML and attachments.

    Args:
        to: List of recipient email addresses.
        subject: Subject line for the message.
        body: Plain text body of the message.
        html: Optional HTML version of the body.
        cc: Optional list of CC recipients.
        bcc: Optional list of BCC recipients.
        attachments: Optional list of attachments.  Each attachment must
            be a dict with keys ``filename``, ``content_type`` and
            ``data``, where ``data`` is a base64-encoded string.

    Returns: The Message-ID assigned to the sent message.
    """
    msg = EmailMessage()
    msg['From'] = APPLE_ID
    msg['To'] = ', '.join(to)
    if cc:
        msg['Cc'] = ', '.join(cc)
    if bcc:
        msg['Bcc'] = ', '.join(bcc)
    msg['Subject'] = subject
    msg.set_content(body or "")
    if html:
        msg.add_alternative(html, subtype='html')
    # Attach files
    if attachments:
        for att in attachments:
            filename = att.get('filename') or 'attachment'
            content_type = att.get('content_type') or 'application/octet-stream'
            data_b64 = att.get('data') or ''
            try:
                binary = base64.b64decode(data_b64)
            except Exception:
                binary = b''
            # Split MIME type into main/subtype
            mtype, _, subtype = content_type.partition('/')
            msg.add_attachment(binary, maintype=mtype, subtype=subtype or 'octet-stream', filename=filename)
    # Send via SMTP
    smtp = _open_smtp()
    try:
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    # Return the message ID for reference
    message_id = msg.get('Message-ID', '') or ''
    return _tool_result({"messageId": message_id})


@mcp.tool()
def create_draft(
    to: Optional[List[str]],
    subject: str,
    body: str,
    html: Optional[str] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    attachments: Optional[List[Dict[str, str]]] = None
) -> ToolResult:
    """
    Create a draft message in the user's Drafts mailbox.

    The message is not sent but stored on the server.  The returned
    Message-ID can later be used to identify the draft.
    """
    # Build the message similarly to send_message
    msg = EmailMessage()
    msg['From'] = APPLE_ID
    if to:
        msg['To'] = ', '.join(to)
    if cc:
        msg['Cc'] = ', '.join(cc)
    if bcc:
        msg['Bcc'] = ', '.join(bcc)
    msg['Subject'] = subject
    msg.set_content(body or "")
    if html:
        msg.add_alternative(html, subtype='html')
    if attachments:
        for att in attachments:
            filename = att.get('filename') or 'attachment'
            content_type = att.get('content_type') or 'application/octet-stream'
            data_b64 = att.get('data') or ''
            try:
                binary = base64.b64decode(data_b64)
            except Exception:
                binary = b''
            mtype, _, subtype = content_type.partition('/')
            msg.add_attachment(binary, maintype=mtype, subtype=subtype or 'octet-stream', filename=filename)
    raw_bytes = msg.as_bytes()
    imap = _open_imap()
    try:
        # Append to Drafts mailbox with the \Draft flag
        flags = '(\\Draft)'
        timestamp = imaplib.Time2Internaldate(time.time())
        imap.append(DRAFTS_MAILBOX, flags, timestamp, raw_bytes)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    message_id = msg.get('Message-ID', '') or ''
    return _tool_result({"messageId": message_id})

def _move_message_impl(mailbox: str, uid: str, dest_mailbox: str) -> bool:
    imap = _open_imap()
    try:
        status, _ = imap.select(mailbox)
        if status != 'OK':
            return False
        # Copy message to destination
        status, _ = imap.uid('COPY', uid, dest_mailbox)
        if status != 'OK':
            return False
        # Mark the original as deleted
        imap.uid('STORE', uid, '+FLAGS', '(\\Deleted)')
        # Permanently remove deleted messages
        imap.expunge()
        return True
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass


@mcp.tool()
def move_message(
    mailbox: str,
    uid: str,
    dest_mailbox: str
) -> ToolResult:
    """
    Move a message from one mailbox to another.

    Args:
        mailbox: Source mailbox containing the message.
        uid: UID of the message to move.
        dest_mailbox: Destination mailbox name.

    Returns: True if the message was successfully moved, False otherwise.
    """
    success = _move_message_impl(mailbox, uid, dest_mailbox)
    return _tool_result({"success": success})


@mcp.tool()
def delete_message(
    mailbox: str,
    uid: str
) -> ToolResult:
    """
    Delete a message from a mailbox.  Deletion is permanent and
    cannot be undone unless the server retains a trash folder.  If
    unsure, use `move_message` to move the message into your trash
    mailbox instead of permanent deletion.
    """
    # Use IMAP store +Flags \Deleted followed by expunge
    imap = _open_imap()
    try:
        status, _ = imap.select(mailbox)
        if status != 'OK':
            return _tool_result({"success": False})
        imap.uid('STORE', uid, '+FLAGS', '(\\Deleted)')
        imap.expunge()
        return _tool_result({"success": True})
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass


@mcp.tool()
def archive_message(
    mailbox: str,
    uid: str
) -> ToolResult:
    """
    Archive a message by moving it into the configured archive mailbox.

    This simply calls `move_message` with the destination set to
    ``ARCHIVE_MAILBOX``.  Returns True on success.
    """
    success = _move_message_impl(mailbox, uid, ARCHIVE_MAILBOX)
    return _tool_result({"success": success})


@mcp.tool()
def flag_message(
    mailbox: str,
    uid: str,
    flag: str,
    value: bool
) -> ToolResult:
    """
    Add or remove a flag from a message.

    Flags are IMAP system flags like ``"\\Seen"`` (read), ``"\\Flagged"``
    (starred), ``"\\Answered"`` (replied) or custom labels.  To mark
    a message as read, pass ``flag="\\Seen"`` and ``value=True``.

    Args:
        mailbox: Mailbox containing the message.
        uid: UID of the message to modify.
        flag: The flag to add or remove.  It must include the
            leading backslash for system flags.
        value: True to add the flag, False to remove it.

    Returns: True if the flag operation succeeded, False otherwise.
    """
    imap = _open_imap()
    try:
        status, _ = imap.select(mailbox)
        if status != 'OK':
            return _tool_result({"success": False})
        op = '+FLAGS' if value else '-FLAGS'
        status, _ = imap.uid('STORE', uid, op, f'({flag})')
        return _tool_result({"success": status == 'OK'})
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
#  Calendar helper functions
#
# The calendar code below mirrors the functionality of the icloud-mcp
# repository.  It depends on the `caldav` package.  If the import at
# the top of this file failed (DAVClient is None), calendar tools
# gracefully return empty results or False.
# ---------------------------------------------------------------------------

def _caldav_client() -> Any:
    """Stateless CalDAV client factory.  Returns None if disabled."""
    if DAVClient is None:
        return None
    return DAVClient(url=CALDAV_URL, username=APPLE_ID, password=ICLOUD_APP_PASSWORD)


def _caldav_principal():
    """Return the authenticated CalDAV principal."""
    cli = _caldav_client()
    if not cli:
        return None
    return cli.principal()


def _caldav_all_calendars():
    """List all calendars for the authenticated principal."""
    principal = _caldav_principal()
    if not principal:
        return []
    try:
        return principal.calendars()
    except Exception as exc:
        log.error("CalDAV list calendars error: %s", exc)
        return []


def _caldav_resolve_calendar(name_or_url: str):
    """Return a caldav.Calendar object from a display name or absolute URL."""
    principal = _caldav_principal()
    if not principal:
        return None
    for c in principal.calendars():
        if getattr(c, 'name', None) == name_or_url or str(c.url) == name_or_url:
            return c
    # Fallback: instantiate by URL directly
    cli = _caldav_client()
    if cli is None:
        return None
    return cli.calendar(url=name_or_url)


def _caldav_parse_iso(s: str) -> dt.datetime:
    """
    Parse an ISO date/time string.  Accepts 'YYYY-MM-DDTHH:MM:SS', or
    the same suffixed with 'Z' (UTC) or an offset like '-05:00'.
    """
    if s.endswith('Z'):
        return dt.datetime.fromisoformat(s[:-1]).replace(tzinfo=dt.timezone.utc)
    return dt.datetime.fromisoformat(s)


def _caldav_fmt(ts: dt.datetime) -> str:
    """Format a datetime for use in an iCalendar DTSTART/DTEND field."""
    return ts.strftime('%Y%m%dT%H%M%S')


def _ics_escape(text: str) -> str:
    """Escape newlines and special characters for iCalendar fields."""
    return (
        text.replace('\\', '\\\\')
            .replace('\n', '\\n')
            .replace(',', '\\,')
            .replace(';', '\\;')
    )


def _caldav_to_iso(value: Any) -> Optional[str]:
    """Format a CalDAV date/time value to ISO if possible."""
    if value is None:
        return None
    try:
        if isinstance(value, dt.datetime):
            return value.isoformat()
        return value.isoformat()  # type: ignore
    except Exception:
        return str(value)


# ---------------------------------------------------------------------------
#  Calendar Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_calendars() -> ToolResult:
    """
    List all calendars available to the authenticated iCloud account.

    Returns a list where each item contains:
    * ``name`` - Display name of the calendar, if available.
    * ``url`` - Absolute CalDAV URL (preferred identifier for other calls).
    * ``id`` - Underlying CalDAV identifier, if exposed by the library.
    If the CalDAV client is not available, an empty list is returned.
    """
    calendars = _caldav_all_calendars()
    out: List[Dict[str, Any]] = []
    for cal in calendars:
        out.append({
            "name": getattr(cal, 'name', None),
            "url": str(cal.url),
            "id": getattr(cal, 'id', None),
        })
    return _tool_result({"calendars": out}, text=f"{len(out)} calendar(s)")


@mcp.tool()
def list_events(
    calendar_name_or_url: str,
    start: str,
    end: str,
    expand_recurring: bool = True
) -> ToolResult:
    """
    List calendar events occurring between two datetimes.  Recurring
    events can be expanded into individual instances by setting
    ``expand_recurring`` to True.

    Args:
        calendar_name_or_url: Display name or absolute CalDAV URL.
        start: ISO datetime string for the start of the range (inclusive).
        end: ISO datetime string for the end of the range (exclusive).
        expand_recurring: Whether to include individual instances of
            recurring events.

    Returns a list of events.  Each event includes its UID, summary,
    start/end in ISO format and the raw iCalendar representation.  If
    CalDAV support is not available, an empty list is returned.
    """
    cal = _caldav_resolve_calendar(calendar_name_or_url)
    if cal is None:
        return _tool_result({"events": []}, text="0 event(s)")
    s = _caldav_parse_iso(start)
    e = _caldav_parse_iso(end)
    try:
        events = cal.search(event=True, start=s, end=e, expand=expand_recurring)
    except Exception as exc:
        log.error("CalDAV list_events error: %s", exc)
        return _tool_result({"events": []}, text="0 event(s)")
    out: List[Dict[str, Any]] = []
    for ev in events:
        comp = ev.component
        summary = str(comp.get('summary', '')) if comp.get('summary') is not None else ''
        dtstart = comp.decoded('dtstart')
        dtend = comp.decoded('dtend', default=None)
        uid = str(comp.get('uid', '')) if comp.get('uid') is not None else ''
        out.append({
            "uid": uid,
            "summary": summary,
            "start": _caldav_to_iso(dtstart),
            "end": _caldav_to_iso(dtend),
            "raw": ev.data,
        })
    return _tool_result({"events": out}, text=f"{len(out)} event(s)")


@mcp.tool()
def create_event(
    calendar_name_or_url: str,
    summary: str,
    start: str,
    end: str,
    tzid: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    url: Optional[str] = None,
    reminders: Optional[List[ReminderSpec]] = None,
) -> ToolResult:
    """
    Create a new calendar event.

    Args:
        calendar_name_or_url: Display name or URL of the target calendar.
        summary: Event title/summary.
        start: ISO datetime when the event begins.
        end: ISO datetime when the event ends.
        tzid: Optional IANA timezone identifier (e.g. ``"America/New_York"``).
        description: Optional event description text.
        location: Optional location string for the event.
        url: Optional URL associated with the event (e.g. meeting link).
        reminders: Optional list of reminder dictionaries. Each reminder can
            contain ``minutes_before_start`` or ``minutes_before_end`` to
            define relative triggers, an optional ``description`` and
            optional ``action`` (defaults to ``DISPLAY``). Supplying
            ``repeat`` and ``duration`` is also supported for repeating
            reminders.

    Returns: The UID assigned to the created event.
    If CalDAV support is unavailable, an empty string is returned.
    """
    cal = _caldav_resolve_calendar(calendar_name_or_url)
    if cal is None:
        return _tool_result({"uid": "", "created": False})
    s = _caldav_parse_iso(start)
    e = _caldav_parse_iso(end)
    tzid = tzid or DEFAULT_TZID
    raw_reminders = [_normalize_reminder_spec(r) for r in (reminders or [])]
    normalised_reminders = [spec for spec in raw_reminders if spec]
    # Generate a random UID using os.urandom.  Append a domain to
    # satisfy iCalendar requirements.
    uid = os.urandom(16).hex() + "@chatgpt-mcp"
    if ICalendarCalendar and ICalendarEvent:
        cal_component = ICalendarCalendar()
        cal_component.add('prodid', '-//ChatGPT MCP iCloud//EN')
        cal_component.add('version', '2.0')
        event_component = ICalendarEvent()
        event_component.add('uid', uid)
        event_component.add('summary', summary)
        event_component.add('dtstart', s)
        event_component.add('dtend', e)
        if tzid:
            try:
                event_component['DTSTART'].params['TZID'] = tzid
                event_component['DTEND'].params['TZID'] = tzid
            except Exception:
                pass
        if description:
            event_component.add('description', description)
        if location:
            event_component.add('location', location)
        if url:
            event_component.add('url', url)
        for spec in normalised_reminders:
            alarm_component = _reminder_spec_to_alarm(spec)
            if alarm_component is not None:
                event_component.add_component(alarm_component)
        cal_component.add_component(event_component)
        ics_data = cal_component.to_ical().decode()
    else:
        ics_lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//ChatGPT MCP iCloud//EN",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DTSTART;TZID={tzid}:{_caldav_fmt(s)}",
            f"DTEND;TZID={tzid}:{_caldav_fmt(e)}",
        ]
        if description:
            ics_lines.append(f"DESCRIPTION:{_ics_escape(description)}")
        if location:
            ics_lines.append(f"LOCATION:{_ics_escape(location)}")
        if url:
            ics_lines.append(f"URL:{_ics_escape(url)}")
        for spec in normalised_reminders:
            ics_lines.extend(_reminder_spec_to_ics_lines(spec))
        ics_lines += ["END:VEVENT", "END:VCALENDAR"]
        ics_data = "\n".join(ics_lines)
    try:
        cal.save_event(ics_data)
    except Exception as exc:
        log.error("CalDAV create_event error: %s", exc)
        return _tool_result({"uid": "", "created": False})
    return _tool_result({"uid": uid, "created": True})


@mcp.tool()
def update_event(
    calendar_name_or_url: str,
    uid: str,
    summary: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    tzid: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    url: Optional[str] = None,
    reminders: Optional[List[ReminderSpec]] = None,
) -> ToolResult:
    """
    Update an existing event identified by its UID.

    Only the fields provided are changed; other fields remain as in
    the original event.  To remove an existing text field, pass an
    empty string (``""``).  For reminders, supplying ``None`` keeps the
    existing alarms, while an empty list removes all reminders.  If an
    event with the given UID cannot be found within ±3 years of the
    current date, False is returned.
    """
    if ICalendarCalendar is None:
        log.error("icalendar package not available; update_event cannot modify events safely")
        return _tool_result({"success": False, "reason": "icalendar-missing"})
    cal = _caldav_resolve_calendar(calendar_name_or_url)
    if cal is None:
        return _tool_result({"success": False, "reason": "calendar-not-found"})
    now = dt.datetime.now(dt.timezone.utc)
    # search window of ±3 years
    s_window = now - dt.timedelta(days=365 * 3)
    e_window = now + dt.timedelta(days=365 * 3)
    target = None
    try:
        for ev in cal.search(event=True, start=s_window, end=e_window, expand=False):
            comp = ev.component
            if str(comp.get('uid', '')) == uid:
                target = ev
                break
    except Exception as exc:
        log.error("CalDAV update_event search error: %s", exc)
        return _tool_result({"success": False, "reason": "search-failed"})
    if target is None:
        return _tool_result({"success": False, "reason": "event-not-found"})
    try:
        calendar_component = ICalendarCalendar.from_ical(target.data)
    except Exception as exc:
        log.error("CalDAV update_event parse error: %s", exc)
        return _tool_result({"success": False, "reason": "parse-failed"})

    event_component = None
    for component in calendar_component.walk('VEVENT'):
        if str(component.get('uid', '')).strip() == uid:
            event_component = component
            break
    if event_component is None:
        return _tool_result({"success": False, "reason": "event-not-found"})

    def _set_text_field(field: str, value: Optional[str]) -> None:
        if value is None:
            return
        if value == "":
            event_component.pop(field, None)
        else:
            event_component[field] = value

    _set_text_field('SUMMARY', summary)
    _set_text_field('DESCRIPTION', description)
    _set_text_field('LOCATION', location)
    _set_text_field('URL', url)

    def _set_datetime_field(field: str, candidate: Optional[str]) -> None:
        if candidate is None:
            return
        previous_tzid: Optional[str] = None
        if field in event_component:
            try:
                previous_tzid = event_component[field].params.get('TZID')
            except Exception:
                previous_tzid = None
        dt_value = _caldav_parse_iso(candidate)
        event_component[field] = dt_value
        if tzid is not None:
            try:
                if tzid:
                    event_component[field].params['TZID'] = tzid
                else:
                    event_component[field].params.pop('TZID', None)
            except Exception:
                pass
        elif previous_tzid:
            try:
                event_component[field].params['TZID'] = previous_tzid
            except Exception:
                pass

    _set_datetime_field('DTSTART', start)
    _set_datetime_field('DTEND', end)

    if tzid is not None and start is None and 'DTSTART' in event_component:
        try:
            if tzid:
                event_component['DTSTART'].params['TZID'] = tzid
            else:
                event_component['DTSTART'].params.pop('TZID', None)
        except Exception:
            pass
    if tzid is not None and end is None and 'DTEND' in event_component:
        try:
            if tzid:
                event_component['DTEND'].params['TZID'] = tzid
            else:
                event_component['DTEND'].params.pop('TZID', None)
        except Exception:
            pass

    if reminders is not None:
        raw_reminders = [_normalize_reminder_spec(r) for r in reminders]
        normalised_reminders = [spec for spec in raw_reminders if spec]
        if not hasattr(event_component, 'subcomponents'):
            event_component.subcomponents = []  # type: ignore[attr-defined]
        event_component.subcomponents = [
            comp for comp in getattr(event_component, 'subcomponents', []) if getattr(comp, 'name', '').upper() != 'VALARM'
        ]
        for spec in normalised_reminders:
            alarm_component = _reminder_spec_to_alarm(spec)
            if alarm_component is not None:
                event_component.add_component(alarm_component)

    try:
        target.data = calendar_component.to_ical().decode()
        target.save()
        return _tool_result({"success": True})
    except Exception as exc:
        log.error("CalDAV update_event error: %s", exc)
        return _tool_result({"success": False, "reason": "update-failed"})


@mcp.tool()
def delete_event(
    calendar_name_or_url: str,
    uid: str
) -> ToolResult:
    """
    Delete an event from the specified calendar by UID.

    Returns True if the event was deleted, False if no matching event
    was found.  If CalDAV support is unavailable, always returns False.
    """
    cal = _caldav_resolve_calendar(calendar_name_or_url)
    if cal is None:
        return _tool_result({"success": False, "reason": "calendar-not-found"})
    now = dt.datetime.now(dt.timezone.utc)
    s_window = now - dt.timedelta(days=365 * 3)
    e_window = now + dt.timedelta(days=365 * 3)
    try:
        for ev in cal.search(event=True, start=s_window, end=e_window, expand=False):
            comp = ev.component
            if str(comp.get('uid', '')) == uid:
                ev.delete()
                return _tool_result({"success": True})
    except Exception as exc:
        log.error("CalDAV delete_event error: %s", exc)
        return _tool_result({"success": False, "reason": "delete-failed"})
    return _tool_result({"success": False, "reason": "event-not-found"})


def _event_instance_key(component: Any) -> str:
    """Build a stable key for de-duplicating recurring instances."""
    uid_val = str(component.get('uid', '') or '').strip()
    recur = component.get('recurrence-id')
    recur_val = str(recur) if recur is not None else ''
    return f"{uid_val}|{recur_val}"


def _iter_time_windows(
    now: dt.datetime,
    start: dt.datetime,
    end: dt.datetime,
    chunk_days: int,
) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Generate forward-then-backward date windows centred on now."""
    windows: List[Tuple[dt.datetime, dt.datetime]] = []
    chunk = dt.timedelta(days=chunk_days)
    cursor = now
    while cursor < end:
        window_end = min(cursor + chunk, end)
        windows.append((cursor, window_end))
        if window_end >= end:
            break
        cursor = window_end
    cursor = now
    while cursor > start:
        window_start = max(cursor - chunk, start)
        windows.append((window_start, cursor))
        if window_start <= start:
            break
        cursor = window_start
    return windows


@mcp.tool()
def search_events(
    query: str,
    scan_days: Optional[int] = None,
    max_results: Optional[int] = None,
    chunk_days: Optional[int] = None,
) -> ToolResult:
    """
    Perform a free-text search across event summaries and descriptions
    around the current date.  Results default to a ±365 day window, but
    callers can override the search horizon by supplying ``scan_days`` or
    setting the ``SCAN_DAYS`` environment variable.  The search stops as
    soon as ``max_results`` hits are collected to keep latency low.

    Returns a list of search hits.  Each hit contains:

    * ``id`` - a composite identifier of the form ``"{calendar_url}|{uid}"``.
    * ``title`` - truncated event summary for display.
    * ``snippet`` - ISO start time and calendar name.
    """
    if DAVClient is None:
        return _tool_result({"results": []}, text="0 result(s)")
    q = (query or '').strip().lower()
    if not q:
        return _tool_result({"results": []}, text="0 result(s)")

    scan_days_val = max(1, scan_days if scan_days is not None else SEARCH_SCAN_DAYS_DEFAULT)
    chunk_days_val = max(1, chunk_days if chunk_days is not None else SEARCH_CHUNK_DAYS_DEFAULT)
    max_hits = max(1, max_results if max_results is not None else SEARCH_MAX_RESULTS_DEFAULT)

    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=scan_days_val)
    end = now + dt.timedelta(days=scan_days_val)

    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for cal in _caldav_all_calendars():
        if len(rows) >= max_hits:
            break
        calname = getattr(cal, 'name', None) or str(cal.url)
        windows = _iter_time_windows(now, start, end, chunk_days_val)
        for win_start, win_end in windows:
            if len(rows) >= max_hits:
                break
            try:
                events = cal.search(event=True, start=win_start, end=win_end, expand=True)
            except Exception as exc:
                log.error("CalDAV search_events error: %s", exc)
                break
            for ev in events:
                if len(rows) >= max_hits:
                    break
                try:
                    comp = ev.component
                except Exception:
                    continue
                key = _event_instance_key(comp)
                if key in seen:
                    continue
                summary = str(comp.get('summary', '') or '')
                descr = str(comp.get('description', '') or '')
                haystack = (summary + '\n' + descr).lower()
                if q not in haystack:
                    continue
                seen.add(key)
                uid_val = str(comp.get('uid', '') or '').strip()
                dtstart = comp.decoded('dtstart')
                when = _caldav_to_iso(dtstart) or ''
                rows.append({
                    'id': f"{str(cal.url)}|{uid_val}",
                    'title': summary[:200],
                    'snippet': f"{when} — {calname}",
                })
    return _tool_result({"results": rows}, text=f"{len(rows)} result(s)")


@mcp.tool()
def fetch_events(
    ids: List[str]
) -> ToolResult:
    """
    Fetch raw iCalendar (ICS) data for a list of composite event IDs.
    The input IDs should be of the form ``"{calendar_url}|{uid}"`` as
    returned by `search_events`.

    Returns a list of dictionaries containing ``id``, ``mimeType``
    (always ``"text/calendar"``) and ``content`` (the raw event data).
    """
    if DAVClient is None:
        return _tool_result({"events": []}, text="0 event(s)")
    ids = ids or []
    calendars = {str(c.url): c for c in _caldav_all_calendars()}
    out: List[Dict[str, Any]] = []
    for ident in ids:
        try:
            cal_url, uid = ident.split('|', 1)
        except ValueError:
            continue
        cal = calendars.get(cal_url)
        if cal is None:
            cal = _caldav_resolve_calendar(cal_url)
            if cal is not None:
                calendars[cal_url] = cal
        if cal is None:
            continue
        found_raw = None
        try:
            event = cal.event_by_uid(uid)
            found_raw = getattr(event, 'data', None)
            if not found_raw:
                try:
                    event.load()
                    found_raw = getattr(event, 'data', None)
                except Exception:
                    found_raw = None
        except Exception:
            continue
        if found_raw:
            out.append({
                'id': ident,
                'mimeType': 'text/calendar',
                'content': found_raw,
            })
    return _tool_result({"events": out}, text=f"{len(out)} event(s)")


# ---------------------------------------------------------------------------
#  Server entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    log.info(
        "Starting MCP HTTP server on %s:%d (IMAP=%s:%d SMTP=%s:%d CalDAV=%s)",
        SERVER_HOST, SERVER_PORT, IMAP_SERVER, IMAP_PORT, SMTP_SERVER, SMTP_PORT, CALDAV_URL
    )
    mcp.run(transport='http', host=SERVER_HOST, port=SERVER_PORT, path='/mcp')