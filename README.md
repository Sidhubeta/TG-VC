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

### 4a. Spotify links (optional)
Pasting a Spotify playlist / album / song link works out of the box (read from Spotify's public page, up to about
100 songs; each song is found on YouTube). For whole playlists, create a free app at https://developer.spotify.com
and set `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`.

### 4b. Full member list for invites (recommended)
Telegram's Bot API can't list a group's members, so by default the Invite list only shows admins and the people
the bot has seen (chatting, reacting, joining the voice chat). To list **everyone** in the group:
1. Open https://my.telegram.org → **API development tools** → create an app (any name).
2. Set `TG_API_ID` and `TG_API_HASH` on Railway. The bot logs in with its own token (no phone number) and only
   reads member lists. The bot must be an admin of the group.
Also turn off privacy mode (`/setprivacy` → Disable in @BotFather) so the bot sees who chats.

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
  them in the group if they never started the bot. The list loads more people as you scroll and searches on
  the server. With `TG_API_ID`/`TG_API_HASH` it shows every member, otherwise everyone the bot has seen. If the
  group hides its members, normal members see only the admins (admins still see everyone); anonymous admins and
  bots are never shown. Invite as many people in a row as you like (up to 10 a minute); the same
  person can be invited again after 5 minutes.
- **Chat:** like Telegram's voice chat: the Message button slides a message box up over the room, messages pop up for
  a few seconds and disappear, tap one to reply, floating reactions. Everyone can read back the messages sent
  since they joined (leaving and coming back starts over; a quick reconnect doesn't count as leaving); admins can
  read back the last 48 hours. Message reactions, delete
- **Music:** YouTube search, links and playlists, synced for everyone, queue, repeat, shuffle, autoplay (only when on),
  vote skip, audio-only/video switch, audio and video quality (Auto adapts to the connection), lyrics,
  Liked Songs and playlists, history, "Made for you" picks from what each person listens to and likes.
  The music keeps going for 10 minutes after the last person leaves, so minimizing Telegram doesn't rewind it.
  Tapping a song gives three clear choices: Play now, Play next, Add to queue. A song is never queued twice.
  Audio and video stay in step with each other and with the room (phones within a few hundredths of a second);
  a song that goes silent restarts by itself, a video that fails to load retries by itself, the next song starts
  even while Telegram is minimized, and if the phone holds the sound back any tap brings it back
- **Crossfade & no silence:** songs start where the sound starts and end where it ends (silent intros and tails are
  skipped), and blend into each other over 4 seconds. Crossfade is on by default; anyone can switch it off for
  themselves (Queue → Crossfade). iPhone switches songs without blending (it can't fade web audio players).
- **Spotify & SoundCloud:** paste a Spotify playlist / album / song link or a SoundCloud song / playlist link (short
  `on.soundcloud.com` links too); the songs are found on YouTube and queued in order.
- **Play history:** what played in the voice chat, who added it and when (More → Play history, or in the queue).
- **/play in the group:** the bot replies with how to join the voice chat and what you can do there.
- **Sound:** Bass Boost, 8D, Concert Hall, Vocal, Slowed + Reverb, Nightcore, plus EQ, reverb and speed.
  Everyone hears the same effect in sync; only admins or the person who added the song can change it.
- **Soundboard:** sounds everyone hears (admins can switch it off)
- **Team:** admins can make people **moderators** (mute / unmute, listener / speaker, remove from the voice chat,
  delete messages — no bans, settings or recording) and **music DJs** (the music rights you choose).
- **Badges:** 🎧 Top DJ, 🎤 Voice of the week, 💬 Chatterbox, 🦉 Night owl, 🌅 Early bird, 🔥 Regular, ⏱ Marathon,
  💿 Music lover, 🌱 New here — earned in each voice chat, shown next to names and in profiles (a new one pops up).
  Times follow `TIMEZONE` (default `Asia/Kolkata`).
- **Health page** (Manage → Health, only for the bot's owner — `OWNER_ID` — and the sudo users they add with `/addsudo` or on that page): bot, YouTube (with a Test button), voice, member list, storage and server at a
  glance, plus problems people's phones reported in the last 24 hours (also written to the server log).
- **Recording chapters:** with each recording the bot also sends who spoke when, the songs, and everyone's talk time.
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

## Music in the voice chat (MUSIC_IN_VOICE=1)
With `MUSIC_IN_VOICE=1` the server joins each voice chat as **Music** and plays the songs there, like a music bot:
the same moment for everyone, no "tap to hear", and it keeps playing with Telegram minimized (like the voices do).
Effects, crossfade, skip, seek and pause are made on the server. Each phone's volume slider and "pause for me" still
work. If a phone's voice isn't connected, that phone plays the music itself as before. The Health page shows
whether it's on and working.

## Music in the group's own Telegram voice chat (MUSIC_IN_TG_VC=1)
A web page can't keep playing once the phone puts Telegram in the background, and an iPhone turns all its sound into
call sound while a mic is on. Telegram's own voice chat has neither problem. With `MUSIC_IN_TG_VC=1` an **assistant
account** (a normal Telegram account, a spare number is best) plays the room's music into the group's Telegram voice
chat; the app stays the remote (queue, search, effects, playlists) and shows a **Listen in Telegram** bar.

1. Set `TG_API_ID` and `TG_API_HASH` (my.telegram.org → API development tools) and `MUSIC_IN_TG_VC=1`.
2. Open the app → More → Manage → **Health** (the bot's owner): under **Music assistant** enter the account's phone
   number, then the code Telegram sends it (and its 2-step password, if it has one). The login is kept in the database.
3. The assistant joins each group by itself (through an invite link the bot makes) and starts the voice chat. To start
   it, it must be an admin that may manage voice chats: the bot makes it one if the bot may add admins; otherwise make
   it an admin yourself, or start the voice chat by hand.

It takes the place of `MUSIC_IN_VOICE` while it's on. The music keeps playing while people listen in Telegram even with
nobody in the app; after 10 minutes without music the assistant leaves the voice chat.
