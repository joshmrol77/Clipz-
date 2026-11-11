# Clipz

Finds the top clips from streamers you're allowed to clip, turns them into vertical
shorts (headline + creator credit, blurred background, normalised audio), and posts the
ones you approve to your YouTube, TikTok and Instagram accounts with one click.

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
| **Streamers** | Add or remove the channels Clipz watches. For each one choose "only the game" or "all their clips" (IRL, chatting, anything), and optionally give them their own hashtags. |
| **Settings** | Paste your Twitch keys, choose the game, turn on autopilot. |
| **Accounts** | Connect YouTube, TikTok and Instagram. |
| **Clips** | Watch each edited clip, then Approve or Reject it. |

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

## Use it from a phone (host it on a server)

A phone can't run Clipz itself (it needs Python and ffmpeg), but it can use Clipz in its
browser if the app runs on a server. The included `Dockerfile` is for that:

1. On a hosting service that builds a GitHub repository's Dockerfile and offers a persistent
   disk, create a service from this repository.
2. Set the environment variable `CLIPZ_PASSWORD` to a long password. Clipz refuses to start
   on a server without one, because the app holds your keys and can post to your accounts.
3. Attach a persistent disk at `/srv/data` so settings, sign-ins and videos survive restarts.
4. Open the service's `https://` address on your phone and sign in with the password.

Hosted this way it also runs around the clock, so autopilot keeps working with no computer on.
The Dockerfile has not been built yet - treat the first deploy as a test.

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
- **YouTube** - no clips API exists, so it takes each creator's recent videos whose title
  matches the keywords and cuts the 45 s "most replayed" section.
- **Kick** - experimental. Kick has no documented clips API; this uses the website's own
  endpoint, which may be blocked or change.

## What has and hasn't been tested

Tested: the editing, the queue, and the app's screens and buttons. **Not yet run against the
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
