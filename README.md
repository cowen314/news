# News digest

A daily headline digest, built from RSS feeds, trimmed by one small AI call, and delivered by email or phone notification. It runs on GitHub Actions, so there's no server and nothing to chat with.

## How it works

**RSS in one paragraph.** Most news sites publish a *feed*: a small, machine-readable file at a fixed URL listing their latest articles (title, link, date, a short summary). It's the same list you'd see on their homepage, minus the page. A "reader" checks those URLs and shows you what's new. That's all this project does, plus some filtering.

Each day at the scheduled time:

1. **Fetch.** `digest.py` downloads every feed in `feeds.txt` (all at once, in a few seconds).
2. **Drop what you've seen.** Anything listed in `seen.json`, or older than 36 hours, is skipped. Each feed contributes at most 30 fresh items by default (set `max=N` after a feed URL to change it).
3. **One model call.** The remaining headlines (usually 50-150) go to a small Claude model, wrapped in `prompt.md`. It returns the IDs to keep per topic, a short note where useful, and the top few stories. It never writes headlines or URLs itself; those come straight from the feeds.
4. **Deliver.** Email gets the full digest grouped by topic. Push gets the same digest in one notification: top stories first, then the rest by topic, trimmed to fit if it runs long.
5. **Mark read.** Every item the model saw is added to `seen.json`, and the workflow commits that file back to the repo. Entries older than 30 days are pruned so it stays small.

If the API key is missing or the call fails, it falls back to "newest N per topic", so you still get a digest.

## Files

| File | What it's for | Edit it? |
|---|---|---|
| `feeds.txt` | Your sources, grouped by topic, with per-topic limits | Often, from your phone |
| `prompt.md` | What you care about and what to skip | When the picks feel off |
| `digest.py` | The script (Python standard library only) | Rarely |
| `.github/workflows/digest.yml` | Schedule and delivery wiring | To change the time |
| `seen.json` | Read-markers, updated automatically | No |

## Setup (about 15 minutes)

1. **Make a private GitHub repo** and upload these files, keeping the `.github/workflows/` folder path. Alternatively, fork this repo.
2. **Get an Anthropic API key** at console.anthropic.com. One digest is a few thousand tokens to a Haiku-class model, a fraction of a cent per day.
3. **Pick delivery and set it up:**
   - **Email (Gmail).** Turn on 2-Step Verification, then create an *app password* at myaccount.google.com/apppasswords. Other providers work too; set `SMTP_HOST` / `SMTP_PORT` in the workflow.
   - **Phone push (ntfy).** Install the free **ntfy** app (iOS/Android) and subscribe to a topic with a long random name, e.g. `digest-k3f9x2qpz7`. Anyone who knows the name can read it, so make it unguessable.
4. **Add repo secrets** (Settings → Secrets and variables → Actions → *Secrets*):

   | Secret | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | your API key |
   | `SMTP_USER` | your Gmail address (email only) |
   | `SMTP_PASSWORD` | the app password (email only) |
   | `EMAIL_TO` | where to send it, if not `SMTP_USER` (optional) |
   | `NTFY_TOPIC` | your topic name (push only) |

5. **Optional repo variables** (same page, *Variables* tab):

   | Variable | Value |
   |---|---|
   | `DELIVERY` | `email` (default), `push`, or `both` |
   | `MODEL` | defaults to `claude-haiku-5-5` |
   | `NTFY_MARKDOWN` | set to `no` if your phone shows raw `[brackets](links)` |

6. **Test it.** Actions tab → *News digest* → **Run workflow**, and pick a delivery. The first run covers the last 36 hours.
7. **Set the time.** The `cron` line in the workflow is in UTC. GitHub's scheduled runs can start several minutes late at busy times.

## Day-to-day, from your phone

- **Add or remove a feed:** edit `feeds.txt` on github.com (or in the GitHub app). It's used from the next run.
- **Switch email ↔ push:** change the `DELIVERY` variable, or pick a delivery when you run it manually.
- **Tune what gets picked:** edit `prompt.md`.

**Finding a feed URL.** Try the site's address with `/feed`, `/rss`, or `/rss.xml` added, or look for an RSS icon. Search "*site name* RSS". For Hacker News, hnrss.org lets you filter, e.g. `https://hnrss.org/frontpage?points=300` for only big stories.

## Running it locally

```sh
python digest.py --dry-run                 # print the digest, send nothing, mark nothing read
ANTHROPIC_API_KEY=… python digest.py --dry-run
```

In VS Code, open `python.code-workspace` and use the Run and Debug panel. **Digest: dry run** reads your key from a `.env` file, which is gitignored. To save your key there, run `python set_key.py`, or the *Set Anthropic API key* task in VS Code (Tasks: Run Task). **Digest: dry run (no AI)** skips the model call.
