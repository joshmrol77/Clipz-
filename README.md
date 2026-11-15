# Clipz

Finds the top clips from streamers you're allowed to clip, turns them into vertical
shorts (headline + creator credit, blurred background, normalised audio), and posts the
ones you approve to your YouTube, TikTok and Instagram accounts with one click.

```
find (Twitch / Kick / YouTube)  ->  download  ->  edit to 9:16  ->  review  ->  post everywhere
```

## Start the app

Needs Python 3.11+ on your computer (ffmpeg comes bundled).

```sh
pip install -e .
clipz app
```

Your browser opens at <http://127.0.0.1:8777>. Everything is done from four tabs:

| Tab | What you do there |
|---|---|
| **Browse** | Every clip Clipz found, best first, filtered by category. Play them right there, then **Edit this clip** or **Skip**. |
| **Edited** | Your picked clips as finished vertical videos: change the layout, then Approve or Reject. |
| **Streamers** | Search for streamers on Twitch, Kick or YouTube as you type, put each in a category (e.g. "GTA 6", "IRL"), choose "only the game" or "all their clips", and optionally give them their own hashtags. |
| **Accounts** | Connect YouTube, TikTok and Instagram. |
| **Settings** | Connect Twitch, choose the game, turn on autopilot or auto-editing. |

The two buttons at the top do the work: **Find new clips** searches, downloads and edits;
**Post approved clips** sends every approved clip to every connected account.

### Layouts

Click any clip to open its editor, where you can retype the headline and switch layout:

- **Framed** - clip in the middle, headline above, blurred backdrop.
- **Just the clip** - the clip fills the whole screen (its sides are cropped).
- **Clip + gameplay** - clip on the top half, a looping gameplay video on the bottom half.
  Press **Add video...** to upload gameplay footage you recorded or have the rights to.

Pressing **Apply and re-edit** rebuilds the video, which then needs approving again. The
layout used for new clips is chosen in Settings.

Autopilot (Settings) repeats the search every N minutes and can skip review entirely. It
runs only while the app is open.

## Use it from your phone (Vercel)

Clipz runs on Vercel's free plan, so it's always on and opens on your phone like an app.

1. **Project**: import this GitHub repository in Vercel (Add New, Project). No build
   settings are needed: Vercel finds `app.py` and `vercel.json`.
2. **Database**: in the project's Storage tab, create a **Neon** Postgres database (free) and
   connect it to the project. This keeps your settings, sign-ins and clip queue.
3. **Video storage**: in the Storage tab, create a **Blob** store with **public** access and
   connect it. Finished videos live here (under long random addresses nobody can guess).
4. **Password**: Settings, Environment Variables: add `CLIPZ_PASSWORD` (a long password of your
   choice) and `CRON_SECRET` (any long random text; Vercel uses it to start the daily search).
5. Redeploy, then open `https://<your-project>.vercel.app` on your phone, sign in, and use
   your browser's **Add to Home Screen** to get the Clipz icon.

Until steps 2-4 are done the site shows a checklist instead of the app, so it never runs
without a password.

How it differs from running on a computer:

- **Daily autopilot**: the free plan allows one scheduled run a day (15:17 UTC; change it in
  `vercel.json`). Press **Find new clips** any time for more.
- **Background work runs in short shifts**: each shift does a few minutes of finding, editing
  or posting, then starts the next. If a hand-off is ever lost, opening the app picks the work
  back up.
- **Gameplay videos are added from a link** (Add from link...), because cloud functions can't
  take large uploads from a phone.
- **Some sites may block cloud servers**: Twitch works through its official API, but YouTube
  and Kick sometimes refuse downloads from data-centre addresses. If that happens, those clips
  fail with an error in the Activity box.

Running it on a computer still works exactly as before (`clipz app`), and the `Dockerfile` is
still there for other hosts.

## One-time account setup

Each platform makes you register a free developer app before software may act for you.

- **Twitch** (to search): <https://dev.twitch.tv/console/apps> -> register an app -> copy
  the client ID and secret into Settings.
- **YouTube** (to post): Google Cloud Console -> new project -> enable *YouTube Data API v3*
  -> create an OAuth client of type *Web application* -> add the redirect address shown in
  Accounts -> paste ID and secret -> **Connect YouTube**. New projects can upload about 6 videos a day until you request more quota.
- **TikTok** (to post): <https://developers.tiktok.com> -> create an app with *Login Kit* and
  *Content Posting API* (Direct Post) -> register the redirect address shown in Accounts ->
  paste key and secret -> **Connect TikTok**. Until TikTok audits your app, posts arrive as
  private and you make them public in the TikTok app.
- **Instagram** (to post): needs a Business or Creator Instagram account.
  <https://developers.facebook.com> -> create an app -> add the *Instagram* product (API setup
  with Instagram login) -> add your account -> **Generate token** -> paste it in Accounts ->
  **Connect Instagram**. To post publicly beyond your own test accounts, Meta may require app review.

## How clips are picked

- **Twitch** - official API: each streamer's clips from the last `lookback_hours` in the
  configured game, ranked by views per hour. Scores refresh on every search, so a clip that
  blows up later moves to the front.
- **YouTube** - no clips API exists, so for each YouTube streamer it looks at their own recent
  videos (cutting the 45 s "most replayed" section) and searches YouTube for today's short
  videos naming them, from any channel - usually other clippers, who are credited in posts.
- **Kick** - experimental. Kick has no documented clips API; this uses the website's own
  endpoint, which may be blocked or change.

## What has and hasn't been tested

Tested: the editing, the queue, the app's screens and buttons, and the cloud version against a
real Postgres database with its background shifts running over real web requests. **Not yet run against the
real services**: Twitch/Kick/YouTube search and download, YouTube upload, and the TikTok
and Instagram connections and uploads (written from their specs). Expect to fix small things on first
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
