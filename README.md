# VC Music — Telegram Mini App voice chat + music

Your own voice chat inside Telegram: voice, video, screen share, live chat, synced YouTube music
(Apple Music style player), lyrics, playlists, effects, admin tools and full join/mic logs.

## What runs where (Railway)

| Service | What it is |
|---|---|
| **app** (this repo) | Python server: Telegram bot, Mini App, chat, music, logs |
| **livekit** | Voice/video server (Railway LiveKit template, TCP mode) |
| **bgutil-provider** *(recommended)* | Gives yt-dlp PO tokens so YouTube blocks less |

## Setup — step by step

### 1. Bot + Mini App in @BotFather
1. You already have the bot token.
2. `/setprivacy` → your bot → **Disable**. Commands work either way, but the **Invite** list can only show
   members the bot has seen chatting in the group (Telegram doesn't let bots read the member list).
3. `/newapp` → pick your bot → title, description, a 640×360 photo →
   **Web App URL** = your Railway app URL (step 3) → **short name** e.g. `vc`.
   Put that short name in `APP_SHORT_NAME`.

### 2. LiveKit on Railway
Railway has no UDP, so LiveKit must run WebRTC over **TCP**. The Railway template
`railway.com/deploy/livekit-voice-agent` ships a LiveKit Server already set up for TCP. Deploy it and keep only the
**LiveKit Server** (and its Redis); delete the agent and frontend services. Copy its public `wss://` URL, API key and
secret into the app variables. Voice works over TCP, just with a little more delay than UDP.

### 3. The app on Railway
1. Push this folder to a GitHub repo → Railway → **New service → GitHub repo**. It builds from the `Dockerfile`.
2. **Add a volume** mounted at `/data` (keeps logs, playlists, bans and the audio cache across deploys).
3. **Generate a domain** (Settings → Networking). Use it as `PUBLIC_URL` and as the Mini App URL in BotFather.
4. Variables (see `.env.example`):
   `BOT_TOKEN`, `APP_SHORT_NAME`, `PUBLIC_URL`, `SECRET_KEY`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.

### 4. YouTube anti-block (recommended)
- New Railway service from Docker image `brainicism/bgutil-ytdlp-pot-provider`, port `4416`.
  Set `POT_PROVIDER_URL=http://<that-service>.railway.internal:4416` on the app.
- If you still get errors: export `cookies.txt` from a **throwaway** YouTube account, base64 it
  (`base64 -w0 cookies.txt`) and put it in `YT_COOKIES_B64`.
- yt-dlp updates itself on every restart.

### 5. Pinned voice chat bar
Send `/vc` in the group. The bot posts a **Voice Chat** photo message with a **JOIN** button and pins it.
It shows up to 3 participants' photos and the count ("5 participants" / "No participants" / "Starts in 2h"),
and is **edited a few seconds after anyone joins or leaves**. It is never deleted. If another message gets pinned,
send `/vc` again to re-pin it. In groups with Topics, it is posted in the topic where you sent `/vc`.
The bot needs the **Pin messages** admin right. Set `VC_LABEL=Video Chat` if you prefer that wording.

### 6. Use it
1. Add the bot to your group and **make it admin** (it needs this to check who is a member).
2. Send `/vc` in the group → tap **Join Voice Chat**.
3. `/resetlink` (admins) kills the old link and posts a new one.

## Security
- The button opens `t.me/<bot>/<app>?startapp=<signed token>`. People only see the Telegram link, not your Railway URL.
- Each group has its own signed token. It doesn't work for another group, and a reset kills old links.
- Every request checks Telegram's signature **and** asks Telegram whether the user is a member of that group.
  Even someone who copies your Railway URL gets nothing: no room, chat, music, streams or logs without a valid member session.
- Bans, room lock, kick and mute-for-everyone are enforced on the server and in LiveKit, not just hidden in the UI.

## Presence & safety extras
- Closing the app leaves the voice chat immediately (and dropped connections are cleaned up within ~20s).
- Every minute the server re-checks membership: anyone who left the Telegram group is removed from the VC.
- Admins get a private message from the bot on reports and join floods (only admins who have started the bot in private).
- Opens fullscreen in Telegram 8.0+ (`FULLSCREEN=1`), and videos support picture-in-picture where the phone allows it.

## Features
- **Voice room:** speaking rings, per-person volume, mute for me, admin mute for everyone, hand raise,
  speaker/listener, camera (asks front or back, can switch), full-screen video, screen share (desktop),
  noise isolation (RNNoise + a soft gate, runs on the phone), sound output picker (Android: phone speaker,
  ear speaker, Bluetooth, earphones; iPhone uses Control Center), voice and music volume sliders,
  weak-internet warnings (yours, and a label on anyone with a weak connection), join/leave sounds, appear as the group
- **Invite:** invite members from inside the voice chat. The bot sends them a private Join button, or mentions
  them in the group if they never started the bot. If the group hides its members, only admins are listed;
  anonymous admins are never shown.
- **Chat:** like Telegram's voice chat: the Message button slides a message box up over the room, messages pop up for
  a few seconds and disappear, tap one to reply, floating reactions. Only admins can read back recent messages
  (kept 48 hours). Message reactions, delete
- **Music:** YouTube search, links and playlists, synced for everyone, queue, repeat, shuffle, autoplay (only when on),
  vote skip, audio-only/video switch, audio and video quality (Auto adapts to the connection), lyrics,
  Liked Songs and playlists, history, "Made for you" picks from what each person listens to and likes.
  The music keeps going for 10 minutes after the last person leaves, so minimizing Telegram doesn't rewind it
- **Sound:** Bass Boost, 8D, Concert Hall, Vocal, Slowed + Reverb, Nightcore, plus EQ, reverb and speed.
  Everyone hears the same effect in sync; only admins or the person who added the song can change it.
- **Soundboard:** sounds everyone hears (admins can switch it off)
- **Music auth:** admins choose what everyone may do with the music (add, play next, vote skip, skip own songs,
  playback, queue, repeat/autoplay, effects on own / any song, video for everyone, stop) and can make chosen people
  music DJs with their own rights. The server enforces it; anything you can't use tells you why in red.
- **Video:** turning a song's video on shows it to everyone (anyone can switch back to audio for themselves);
  480p by default and it never changes by itself; fills the player edge to edge, full screen turns it sideways
- **Admin:** title/description, lock, new participants can speak, music permissions and DJs, mute everyone,
  invite link copy/reset, recording (named, renamable while recording; the bot sends the file only to the
  admin who recorded it, in private), scheduled reminders
- **Logs:** who joined, when they left, how long they stayed, mic on and off time. Search by name, @username or ID.
  Filter by today/7/30 days/custom, ranking by time/joins/mic, and full visit history per person
- **Safety:** members can report. People who join in a flood, rejoin over and over, or get reported are flagged
  for admins, and admins decide whether to ban. Ban list with unban.

## Local test (no Telegram)
```
pip install -r requirements.txt
DEV_MODE=1 BOT_TOKEN=1:x python -m app.main
# open http://localhost:8000/?dev=1&uid=1  (admin)   and  ?dev=1&uid=2  (member)
```
Never set `DEV_MODE=1` on Railway.
