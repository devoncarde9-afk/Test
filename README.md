# bait-bot

A Discord bot with a single slash command, `/bait`, that:

1. Takes an audio file attachment.
2. Prepends `assets/intro.mp3`.
3. Speed-changes the file to 1.4x or 1.7x (your choice via the command's
   dropdown) — pitch rises with speed, like a turntable/tape speed-up.
4. Appends `assets/outro.mp3`.
5. Exports the result as a single OGG (Vorbis) file and replies with it.

No loudness/decoder-table trick is involved — this is a plain speed change
and concatenation, so the output plays normally in any player.

## 1. Create the Discord application

1. Go to https://discord.com/developers/applications → **New Application**.
2. Under **Bot**, click **Add Bot**, then **Reset Token** and copy it. This
   is your `DISCORD_TOKEN` — keep it secret, never commit it to the repo.
3. Under **Bot**, make sure **Message Content Intent** is OFF (not needed —
   the bot only uses slash commands and attachments).
4. Under **OAuth2 → URL Generator**, check scopes `bot` and
   `applications.commands`, and under bot permissions check
   **Send Messages** and **Attach Files**. Use the generated URL to invite
   the bot to your private server.

## 2. Repo layout

```
bait-bot/
  bot.py
  requirements.txt
  Dockerfile
  assets/
    intro.mp3      <- your intro bait clip
    outro.mp3       <- your rblx bait clip
```

Put your two clips in `assets/` with those exact filenames (or edit the
`INTRO_PATH` / `OUTRO_PATH` constants in `bot.py` if you'd rather name them
differently).

**Important:** since this repo will contain your bait audio files, keep the
GitHub repo **private**. Anyone with read access to the repo can download
`assets/` directly.

## 3. Configure the token

Never hardcode the token in `bot.py`. Set it as an environment variable
called `DISCORD_TOKEN` wherever you host it:

- **Locally:** `export DISCORD_TOKEN=your-token-here` before running.
- **A VPS / your own machine:** put it in a `.env` file and load it, or set
  it in your shell profile / systemd service file.
- **Railway / Fly.io / Render:** set it as a "Secret" or "Environment
  Variable" in that platform's dashboard — most of these platforms can
  deploy straight from a GitHub repo and will build the included
  `Dockerfile` automatically.

GitHub Pages / GitHub itself does **not** run background processes, so you
can't "host from GitHub" directly — GitHub is just where the code lives.
You still need somewhere that keeps a process running continuously:

- A small VPS you already have (DigitalOcean, a home server, etc.) — clone
  the repo there and run it with Docker or directly with Python.
- A platform with a free/cheap tier that deploys from GitHub automatically,
  such as Railway or Fly.io.

## 4. Running it

### With Docker (recommended — bundles ffmpeg for you)

```bash
docker build -t bait-bot .
docker run -e DISCORD_TOKEN=your-token-here bait-bot
```

### Without Docker

You need `ffmpeg` installed and on your `PATH` (`apt install ffmpeg`,
`brew install ffmpeg`, etc.), then:

```bash
pip install -r requirements.txt
export DISCORD_TOKEN=your-token-here
python bot.py
```

## 5. Using it

In your server, type `/bait`, attach the audio file, and pick `1.4x` or
`1.7x` from the speed dropdown. The bot will reply with `bait.ogg` once
it's done (usually a few seconds for short files).

## Notes / limits

- Discord's default upload limit is 25 MB (higher with server boosts). The
  bot checks the output size and tells you if it's too big instead of
  failing silently — if that happens, use a shorter input file.
- The intro/outro clips are re-encoded each run to match the main track's
  sample rate, so they'll always glue together cleanly regardless of the
  input file's original format.
- If you want other speed options, add more `app_commands.Choice(...)`
  entries in `bot.py`.
