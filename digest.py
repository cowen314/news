#!/usr/bin/env python3
"""
Daily news digest: RSS/Atom feeds -> one small-model filtering call -> email and/or phone push.

Standard library only, so the GitHub Action has nothing to install.

  python digest.py                  # build, send, and mark items read
  python digest.py --dry-run        # print the digest; send nothing, mark nothing read
  python digest.py --delivery push  # override the DELIVERY setting for this run
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.entities import name2codepoint
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FEEDS_FILE = ROOT / "feeds.txt"
PROMPT_FILE = ROOT / "prompt.md"
SEEN_FILE = ROOT / "seen.json"

LOOKBACK_HOURS = 36      # ignore anything older than this, even if never seen
SEEN_TTL_DAYS = 30       # forget read-markers after this long, so seen.json stays small
DEFAULT_FEED_CAP = 30    # max fresh items taken from any one feed before the model sees them
SUMMARY_CHARS = 180      # snippet length passed to the model (keeps the prompt cheap)
PUSH_LIMIT_BYTES = 3900  # ntfy's limit is 4096 bytes per message
USER_AGENT = "news-digest/1.0 (personal RSS digest)"
NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------- feeds.txt

def load_topics(path: Path = FEEDS_FILE) -> list[dict]:
    """Parse feeds.txt: [Topic] headers with optional max=N, then one feed URL per line."""
    topics: list[dict] = []
    current = None
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()  # comments: whole-line or after a space
        if not line:
            continue
        header = re.match(r"^\[(.+?)\]\s*(.*)$", line)
        if header:
            opts = dict(re.findall(r"(\w+)=(\S+)", header.group(2)))
            current = {"name": header.group(1).strip(), "max": int(opts.get("max", 6)), "feeds": []}
            topics.append(current)
            continue
        parts = line.split()
        if not parts[0].startswith(("http://", "https://")):
            print(f"feeds.txt line {lineno}: not a URL, skipped: {raw!r}", file=sys.stderr)
            continue
        if current is None:
            current = {"name": "General", "max": 6, "feeds": []}
            topics.append(current)
        opts = dict(re.findall(r"(\w+)=(\S+)", " ".join(parts[1:])))
        current["feeds"].append({"url": parts[0], "cap": int(opts.get("max", DEFAULT_FEED_CAP))})
    return [t for t in topics if t["feeds"]]


# ---------------------------------------------------------------- fetching & parsing

def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _parse_date(s: str) -> datetime | None:
    s = s.strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)          # RSS: "Mon, 12 Oct 2026 08:00:00 GMT"
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))  # Atom/RDF: ISO 8601
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _clean(s: str, limit: int) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


def parse_feed(data: bytes) -> tuple[str, list[dict]]:
    """Handles RSS 2.0, RSS 1.0 (RDF) and Atom by matching element names, ignoring namespaces."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # Many feeds use HTML entities (&nbsp;) that aren't legal XML: convert those to numeric
        # character references, escape any remaining stray ampersands, and retry.
        text = data.decode("utf-8", errors="replace")
        text = re.sub(r"&([a-zA-Z]\w*);",
                      lambda m: m.group(0) if m.group(1) in ("amp", "lt", "gt", "quot", "apos")
                      else (f"&#{name2codepoint[m.group(1)]};" if m.group(1) in name2codepoint else m.group(0)),
                      text)
        text = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
        root = ET.fromstring(text)

    channel = _child(root, "channel")
    feed_title = _clean(_text(_child(channel if channel is not None else root, "title")), 80)

    entries = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = _clean(_text(_child(el, "title")), 300)
        link = ""
        for c in el:
            if _local(c.tag) == "link":
                href = c.get("href")
                if href and c.get("rel", "alternate") == "alternate":
                    link = href
                    break
                if not href and (c.text or "").strip():
                    link = c.text.strip()
                    break
        if not link:
            link = el.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", "")
        guid = _text(_child(el, "guid", "id")) or link or title
        date = None
        for name in ("published", "pubdate", "date", "updated", "issued"):
            date = _parse_date(_text(_child(el, name)))
            if date:
                break
        summary = _text(_child(el, "description", "summary", "content", "encoded"))
        if title and link:
            entries.append({
                "id": hashlib.sha1(guid.encode()).hexdigest()[:16],
                "title": title,
                "link": link,
                "date": date,
                "summary": _clean(summary, SUMMARY_CHARS),
            })
    return feed_title, entries


def collect(topics: list[dict], seen: dict) -> tuple[list[dict], set[str]]:
    """Fetch every feed in parallel; keep unseen, recent items, capped per feed.
    Also returns the IDs of every fresh item, including ones the cap dropped, so they count as read."""
    jobs = [(t, f) for t in topics for f in t["feeds"]]
    cutoff = NOW - timedelta(hours=LOOKBACK_HOURS)

    def work(job):
        topic, feed = job
        try:
            source, entries = parse_feed(fetch(feed["url"]))
        except (urllib.error.URLError, ET.ParseError, OSError, ValueError) as e:
            print(f"  ! {feed['url']}: {e}", file=sys.stderr)
            return []
        fresh = [e for e in entries
                 if e["id"] not in seen and (e["date"] is None or e["date"] >= cutoff)]
        fresh.sort(key=lambda e: e["date"] or NOW, reverse=True)
        for e in fresh:
            e["topic"] = topic["name"]
            e["source"] = source or re.sub(r"^https?://(www\.)?([^/]+).*", r"\2", feed["url"])
        print(f"  {len(fresh):3d} new / {len(entries):3d}  {feed['url']}", file=sys.stderr)
        return fresh

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, jobs))

    items, ids, all_fresh = [], set(), set()
    for (_, feed), fresh in zip(jobs, results):
        all_fresh.update(e["id"] for e in fresh)
        for e in fresh[: feed["cap"]]:
            if e["id"] not in ids:          # the same story can appear in two feeds
                ids.add(e["id"])
                items.append(e)
    return items, all_fresh


# ---------------------------------------------------------------- the one model call

def select_with_model(items: list[dict], topics: list[dict]) -> dict | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not items:
        return None
    budgets = "\n".join(f"- {t['name']}: up to {t['max']}" for t in topics)
    listing = "\n".join(
        f"[{i}] ({e['topic']} | {e['source']}) {e['title']}" + (f" — {e['summary']}" if e["summary"] else "")
        for i, e in enumerate(items)
    )
    prompt = (PROMPT_FILE.read_text(encoding="utf-8")
              .replace("{{date}}", NOW.strftime("%A %d %B %Y"))
              .replace("{{budgets}}", budgets)
              .replace("{{items}}", listing))
    body = json.dumps({
        "model": os.environ.get("MODEL") or "claude-haiku-5-5",
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            reply = json.load(resp)
        text = "".join(b.get("text", "") for b in reply.get("content", []))
        usage = reply.get("usage", {})
        print(f"model: {usage.get('input_tokens')} tokens in, {usage.get('output_tokens')} out", file=sys.stderr)
        result = json.loads(text[text.index("{"): text.rindex("}") + 1])
        valid = range(len(items))
        result["keep"] = [k for k in result.get("keep", []) if isinstance(k, dict) and k.get("id") in valid]
        result["top"] = [i for i in result.get("top", []) if i in valid]
        return result if result["keep"] else None
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"model call failed, using plain recency instead: {e}", file=sys.stderr)
        return None


def select_by_recency(items: list[dict], topics: list[dict]) -> dict:
    """Fallback when there's no API key or the call fails: newest N per topic."""
    keep, top = [], []
    for t in topics:
        idx = [i for i, e in enumerate(items) if e["topic"] == t["name"]][: t["max"]]
        keep += [{"id": i} for i in idx]
        top += idx[:1]
    return {"keep": keep, "top": top}


# ---------------------------------------------------------------- rendering & delivery

def build(items: list[dict], topics: list[dict], choice: dict) -> dict:
    notes = {k["id"]: (k.get("note") or "").strip() for k in choice["keep"]}
    by_topic = {t["name"]: [] for t in topics}
    for k in choice["keep"]:
        by_topic[items[k["id"]]["topic"]].append(k["id"])
    return {"top": [i for i in choice["top"] if i in notes][:6], "by_topic": by_topic,
            "notes": notes, "headline": (choice.get("headline") or "").strip()}


def render_text(items, d) -> str:
    out = [f"News digest — {NOW:%a %d %b %Y}", ""]
    if d["headline"]:
        out += [d["headline"], ""]
    for name, ids in d["by_topic"].items():
        if ids:
            out.append(name.upper())
            for i in ids:
                e = items[i]
                out.append(f"• {e['title']} ({e['source']})")
                if d["notes"][i]:
                    out.append(f"  {d['notes'][i]}")
                out.append(f"  {e['link']}")
            out.append("")
    return "\n".join(out)


def render_html(items, d) -> str:
    esc = html.escape
    def li(i):
        e = items[i]
        note = f'<div style="color:#555;font-size:14px;margin-top:2px">{esc(d["notes"][i])}</div>' if d["notes"][i] else ""
        return (f'<li style="margin:0 0 10px"><a href="{esc(e["link"])}" style="color:#1a4fd6;text-decoration:none;'
                f'font-weight:600">{esc(e["title"])}</a> <span style="color:#888;font-size:13px">'
                f'{esc(e["source"])}</span>{note}</li>')
    parts = [f'<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;'
             f'margin:auto;color:#222;line-height:1.4"><h2 style="margin:0 0 4px">News digest</h2>'
             f'<div style="color:#888;margin-bottom:16px">{NOW:%A %d %B %Y}</div>']
    if d["headline"]:
        parts.append(f'<p style="margin:0 0 16px">{esc(d["headline"])}</p>')
    for name, ids in d["by_topic"].items():
        if ids:
            parts.append(f'<h3 style="margin:20px 0 8px;font-size:13px;letter-spacing:.08em;'
                         f'text-transform:uppercase;color:#666">{esc(name)}</h3><ul style="padding-left:18px;margin:0">')
            parts += [li(i) for i in ids]
            parts.append("</ul>")
    parts.append("</div>")
    return "".join(parts)


def render_push(items, d) -> list[str]:
    """The whole digest: headline, top stories, then the rest by topic, split into as many
    ntfy messages as it takes. A topic only spans two messages if it can't fit in one alone."""
    md = push_markdown()
    def line(i):
        e = items[i]
        return f"• [{e['title']}]({e['link']}) · {e['source']}" if md else f"• {e['title']} · {e['source']}\n  {e['link']}"
    def heading(name):
        return f"**{name}**" if md else name.upper()

    top = d["top"]
    sections = [("Top", list(top))] + [(name, [i for i in ids if i not in top]) for name, ids in d["by_topic"].items()]

    blocks = [d["headline"]] if d["headline"] else []
    for name, ids in sections:
        block = heading(name)
        for i in ids:
            if len((block + "\n" + line(i)).encode()) > PUSH_LIMIT_BYTES:
                blocks.append(block)
                block = heading(f"{name} (cont.)")
            block += "\n" + line(i)
        if ids:
            blocks.append(block)

    messages = []
    for block in blocks:
        if messages and len((messages[-1] + "\n\n" + block).encode()) <= PUSH_LIMIT_BYTES:
            messages[-1] += "\n\n" + block
        else:
            messages.append(block)
    return messages


def push_markdown() -> bool:
    return os.environ.get("NTFY_MARKDOWN", "yes").lower() not in ("no", "false", "0")


def send_email(subject: str, text: str, html_body: str) -> None:
    user, password = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, user
    msg["To"] = os.environ.get("EMAIL_TO") or user
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", 587))) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)


def send_push(title: str, body: str) -> None:
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(
        f"{server}/{os.environ['NTFY_TOPIC']}", data=body.encode(), method="POST",
        headers={"Title": title, "Markdown": "yes" if push_markdown() else "no", "Tags": "newspaper"},
    )
    urllib.request.urlopen(req, timeout=20).read()


# ---------------------------------------------------------------- main

def load_seen() -> dict:
    try:
        return json.loads(SEEN_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_seen(seen: dict, ids: set[str]) -> None:
    stamp = NOW.strftime("%Y-%m-%d")
    for i in ids:                         # everything fresh today counts as read, picked or not
        seen[i] = stamp
    cutoff = (NOW - timedelta(days=SEEN_TTL_DAYS)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_FILE.write_text(json.dumps(dict(sorted(seen.items(), key=lambda kv: kv[1])), indent=0) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delivery", choices=["email", "push", "both"], help="overrides the DELIVERY env var")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending; don't mark anything read")
    args = ap.parse_args()
    delivery = args.delivery or os.environ.get("DELIVERY") or "email"

    topics = load_topics()
    seen = load_seen()
    print(f"fetching {sum(len(t['feeds']) for t in topics)} feeds…", file=sys.stderr)
    items, fresh_ids = collect(topics, seen)
    print(f"{len(items)} unread items", file=sys.stderr)
    if not items:
        print("nothing new; no digest sent", file=sys.stderr)
        return 0

    choice = select_with_model(items, topics) or select_by_recency(items, topics)
    d = build(items, topics, choice)
    subject = f"News digest · {NOW:%a %d %b}"
    text, html_body = render_text(items, d), render_html(items, d)

    pushes = render_push(items, d)
    if args.dry_run:
        print(text)
        for n, body in enumerate(pushes, 1):
            print(f"\n--- push {n}/{len(pushes)} ({len(body.encode())} bytes) ---\n{body}")
        return 0

    if delivery in ("email", "both"):
        send_email(subject, text, html_body)
        print("email sent", file=sys.stderr)
    if delivery in ("push", "both"):
        for n, body in enumerate(pushes, 1):
            send_push(subject + (f" ({n}/{len(pushes)})" if len(pushes) > 1 else ""), body)
        print(f"push sent ({len(pushes)} messages)", file=sys.stderr)
    save_seen(seen, fresh_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
