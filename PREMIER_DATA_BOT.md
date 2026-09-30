# Premier Data bot

Give this file to Grok. Set up a bot named **Premier Data**. The Python beside this file does the checking. You already know how to sign in to mypremierdata.com. Do not ask for the password, and do not put the password in the script.

## What the script does

`premier_data_watch.py` reads two pages inside the account that is already signed in:

- the callback list
- the appointments that showed

It saves a local snapshot. On the next run it compares. It sends JSON to a webhook only when that list changed. A run with no change prints one line and does not call a webhook.

Run it every 15 minutes from cron or launchd. Do not open the website yourself on that schedule. Do not spend tokens reading those pages every 15 minutes. The script is the checker.

## Open fields

These are blank on purpose. Thane will tell you the ones he has. Look at the signed-in site once, only to fill a field he did not already give you. Then stop looking.

Copy `premier_data_config.example.json` to `premier_data_config.json` and fill it in.

| Field | What it is | Status |
| --- | --- | --- |
| `callback_list_url` | Full URL of the callback list after sign-in | OPEN |
| `showed_appointments_url` | Full URL of the showed-appointments list after sign-in | OPEN |
| `callback_webhook_url` | Where a callback change is sent | OPEN |
| `showed_webhook_url` | Where a showed-appointment change is sent | OPEN |
| `cookie_header` | Cookie header from the browser that is already signed in. Use this or `cookie_file`, not both required | OPEN |
| `cookie_file` | Netscape cookie file from that same signed-in browser | OPEN |
| `next_link_contains` | Only if a list has a real Next link. A short unique bit of that link’s href or label. Leave blank if the list is one page | OPEN |
| `max_pages` | How many pages to follow. Leave `1` until paging is known | OPEN |
| `minimum_columns` | Smallest table that counts as the list. Leave `2` unless the real table is narrower | OPEN |
| `fire_webhook_on_first_run` | Leave `false`. The first run only saves a snapshot | OPEN if Thane wants the first run to send |

If a list is paged with an ASP.NET postback button and there is no normal link, leave `next_link_contains` blank and tell Thane the list is only the first page until he tells you otherwise. Do not guess the button name.

## Schedule

Do not use a model loop. Use the clock.

```cron
*/15 * * * * cd /path/to/this/folder && /usr/bin/python3 premier_data_watch.py >> premier_watch.log 2>&1
```

Replace the folder path. The log should stay short: one line per list per run.

When the script prints `SESSION_EXPIRED`, the saved cookie is dead. Sign in again the way you already know, update `cookie_header` or `cookie_file`, and leave the schedule alone. Do not sign in on every run.

## Webhook body

The script posts this JSON itself. Do not reformat it with a model.

```json
{
  "source": "mypremierdata.com",
  "list": "callbacks",
  "page_url": "",
  "count": 0,
  "added_count": 0,
  "removed_count": 0,
  "added": [],
  "removed": [],
  "rows": []
}
```

`list` is `callbacks` or `showed_appointments`.

## What you must send back to Thane

After it is set up, reply with this and fill every line. Do not say it is done if a line is still unknown.

1. Bot name: Premier Data
2. Callback list URL you used:
3. Showed appointments URL you used:
4. Callback webhook host only (not the full secret URL):
5. Showed webhook host only (not the full secret URL):
6. How the signed-in cookie is stored (`cookie_header` or `cookie_file`):
7. Paging: one page, or the next-link text you set:
8. The scheduler command, and that it is every 15 minutes:
9. Result of one manual run (the printed lines). Say whether a webhook was sent. The first run should say `first snapshot saved` and must not send a webhook.
10. Confirm you are not reading the site with the model on the 15-minute schedule.
11. Anything still OPEN that you need from Thane:
