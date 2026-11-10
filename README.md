# Clipz

Finds the top clips from streamers you're allowed to clip, turns them into vertical
shorts (headline + creator credit, blurred background, normalised audio), and posts the
ones you approve to your YouTube and TikTok accounts.

```
find (Twitch / Kick / YouTube)  ->  download  ->  edit to 9:16  ->  review  ->  post everywhere
```

## Start the app

Needs Python 3.11+ and ffmpeg 6.1+ on your computer.

```sh
pip install -e .
clipz app
```

Your browser opens at <http://127.0.0.1:8777>. Everything is done from four tabs:

| Tab | What you do there |
|---|---|
| **Streamers** | Add or remove the channels Clipz watches. |
| **Settings** | Paste your Twitch keys, choose the game, turn on autopilot. |
| **Accounts** | Connect YouTube and TikTok. |
| **Clips** | Watch each edited clip, then Approve or Reject it. |

The two buttons at the top do the work: **Find new clips** searches, downloads and edits;
**Post approved clips** sends every approved clip to every connected account.

Autopilot (Settings) repeats the search every N minutes and can skip review entirely. It
runs only while the app is open.

## One-time account setup

Each platform makes you register a free developer app before software may act for you.

- **Twitch** (to search): <https://dev.twitch.tv/console/apps> -> register an app -> copy
  the client ID and secret into Settings.
- **YouTube** (to post): Google Cloud Console -> new project -> enable *YouTube Data API v3*
  -> create an OAuth client of type *Desktop app* -> paste ID and secret in Accounts ->
  **Connect YouTube**. New projects can upload about 6 videos a day until you request more quota.
- **TikTok** (to post): <https://developers.tiktok.com> -> create an app with *Login Kit* and
  *Content Posting API* (Direct Post) -> register the redirect address shown in Accounts ->
  paste key and secret -> **Connect TikTok**. Until TikTok audits your app, posts arrive as
  private and you make them public in the TikTok app.
- **Instagram**: not supported yet (it only accepts videos hosted at a public web address).
  Post the files in `data/out/` by hand.

## How clips are picked

- **Twitch** - official API: each streamer's clips from the last `lookback_hours` in the
  configured game, ranked by views per hour. Scores refresh on every search, so a clip that
  blows up later moves to the front.
- **YouTube** - no clips API exists, so it takes each creator's recent videos whose title
  matches the keywords and cuts the 45 s "most replayed" section.
- **Kick** - experimental. Kick has no documented clips API; this uses the website's own
  endpoint, which may be blocked or change.

## What has and hasn't been tested

Tested: the editing, the queue, and the app's screens and buttons. **Not yet run against the
real services**: Twitch/Kick/YouTube search and download, YouTube upload, and the TikTok
connection and upload (written from TikTok's spec). Expect to fix small things on first
real use.

## Permission

Clipping someone's stream without their permission is copyright infringement and the usual
outcome is strikes and a terminated channel. Only add creators who run a clipping program or
have told you it's fine, and keep the on-video credit.

## Command line (optional)

Everything the app does is also available as commands: `clipz run`, `clipz list`,
`clipz approve <id>|all`, `clipz reject <id>`, `clipz publish`, `clipz retry`, and
`clipz add <url> --title "..." --credit "..."` to queue a single clip by hand.

## Tests

```sh
pip install pytest && pytest
```
