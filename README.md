# Clipz

Finds the top clips from streamers you're allowed to clip, turns them into vertical
shorts (headline + creator credit, blurred background, normalised audio), and uploads the
ones you approve to YouTube Shorts.

```
discover (Twitch / Kick / YouTube)  ->  download  ->  render 9:16  ->  review  ->  publish
```

## Setup

Needs Python 3.11+ and ffmpeg 6.1+.

```sh
pip install -e '.[youtube]'
cp config.example.toml config.toml   # then edit it
```

1. **Creators** - list the channels you have permission to clip under `[[creators]]`.
   Discovery only ever looks at these channels.
2. **Twitch** - register an app at <https://dev.twitch.tv/console/apps> and paste the
   client id/secret into `[twitch]`.
3. **YouTube upload** - in Google Cloud console enable *YouTube Data API v3*, create an
   OAuth client of type *Desktop app*, and save the JSON as `client_secret.json`.
   The first `clipz publish` opens a browser to authorise your channel.

## Daily use

```sh
clipz run                    # find, download and render the best new clips
clipz list --status rendered # see what's waiting; videos are in data/out/
clipz approve <id> [<id>..]  # or: clipz approve all
clipz reject <id>
clipz publish                # upload approved clips (default max 3 per run)
clipz retry                  # re-process anything that failed
```

Add a single clip by hand (any site yt-dlp supports):

```sh
clipz add "https://kick.com/someone?clip=clip_123" --title "He landed it first try" --credit kick.com/someone
clipz add "https://youtu.be/VIDEOID" --title "..." --credit "youtube.com/@x" --start 754 --end 790
```

To run unattended, set `auto_approve = true` and schedule `clipz run && clipz publish` with cron.

## How clips are picked

- **Twitch** - official API: each creator's clips from the last `lookback_hours` in the
  configured game, ranked by views per hour.
- **YouTube** - no clips API exists, so it takes each creator's recent videos whose title
  matches `keywords` and cuts the 45 s "most replayed" section.
- **Kick** - experimental. Kick has no documented clips API; this uses the website's own
  endpoint, which may be blocked or change. `clipz add` always works as a fallback.

## Limits

- Publishing covers YouTube Shorts only. TikTok and Instagram require an app review before
  their APIs allow posting; until then, upload the files in `data/out/` by hand.
- No burned-in speech captions yet.
- Clipping someone's stream without their permission is copyright infringement and the usual
  outcome is strikes and a terminated channel. Only list creators who run a clipping program
  or have told you it's fine, and keep the on-video credit.

## Tests

```sh
pip install pytest && pytest
```
