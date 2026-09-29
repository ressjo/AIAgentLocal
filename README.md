# Orbwise – a local AI voice assistant for Linux

A voice assistant that runs **entirely on your own Linux PC**: an animated HUD web interface, voice input
via wake word ("Hey Jarvis") or push-to-talk, spoken answers, a local LLM (Ollama or llama.cpp), **persistent
memory in plain Markdown files** and real control over your system – updates, packages, files, NAS, apps,
services, network, smart home, documents, notes and calendar.

German and English are supported (`language: de|en`). Out of the box the assistant introduces itself as
**"Jarvis"** and listens for "Hey Jarvis" – that is just its default persona; give it any name with
`assistant_name` in the config.

![Orbwise web interface](docs/screenshot.png)
<sub>Screenshot in demo mode (`ORBWISE_FAKE_LLM=1`, no real model attached).</sub>

```
Browser (localhost:8765)                          Python backend (FastAPI, 127.0.0.1 only)
 ├─ neural-network orb (canvas)         ◄──WS──►  Agent ── tool loop ──► tools (confirm dangerous ones)
 ├─ chat · activity · history · memory             │
 ├─ microphone → 16 kHz PCM             ──WS──►   wake word (openWakeWord) → VAD → Whisper
 └─ playback + level → orb              ◄──────   Piper TTS (sentence by sentence)
                                                   │
                         Ollama / llama-server (LLM + bge-m3)   ~/.local/share/orbwise/memory/
```

> Orbwise is an independent hobby project. Its default persona "Jarvis" is a nod to the film assistant; the project
> is not affiliated with or endorsed by Marvel or Disney and does not ship the film voice or any other copyrighted
> material.
>
> ⚠ Orbwise can run commands on your computer (with root, if you allow it). Use it at your own risk – see the
> [Disclaimer](#disclaimer).

## Features

| Area | What Orbwise can do |
|---|---|
| **Voice** | "Hey Jarvis" wake word, microphone button or **hold the space bar** (push-to-talk). Answers are spoken sentence by sentence while the model is still writing; interrupt at any time. |
| **System** | Full system update (Arch: `pacman -Syu` + AUR via yay/paru, Debian/Ubuntu: `apt`), list/search/install/remove packages, system info, shutdown/reboot/suspend/lock |
| **System & network** | Top processes and killing them, systemd services (status, start/stop/restart, logs), IP/gateway/DNS/Wi-Fi, ping, open ports, port checks, disk usage and clean-up |
| **Files** | Find files by name (plocate/fd) or content (ripgrep), list folders, read/write text files, open files and URLs – also on a mounted **NAS** |
| **Apps & web** | Start installed applications, open websites (with your own shortcuts), web search (official **Brave Search API**, your own SearXNG, or scraping via ddgs), read web pages |
| **Shell** | Any bash command – read-only ones run directly, changing ones only after confirmation, destructive ones never |
| **Everyday** | Weather (Open-Meteo), reminders and timers, a **morning briefing** with the items you choose (incl. news and your Paperless inbox) |
| **Routines** | Tasks Orbwise does on its own at set times – "every weekday at 8, search Linux news" – each with its own chat |
| **Home Assistant** | Find devices by name/room/type, read sensors, switch/dim lights, heating, covers, scenes – locks, alarms and gates only after confirmation |
| **Paperless-ngx** | Search documents, **ask questions about their content**, open them as PDF, suggest and apply correspondent, type, tags, title and date (after confirmation) |
| **E-mail** | **Proton Mail** (via the Proton Mail Bridge) or any IMAP mailbox: unread mails, search, read, ask about a mail, archive/move/label/trash, PDF attachments → Paperless (changes after confirmation); optional sending with an edit-and-confirm dialog |
| **Telegram** | Chat with Orbwise from your phone (text or voice messages), reminders arrive on the phone, confirmations via buttons |
| **Obsidian** | Search, read and ask questions about notes in your vault, create/append/update notes, open them in Obsidian |
| **Trilium** | Search and read notes, create notes in the inbox, append to notes |
| **Calendar** | iCloud or any CalDAV server: list events, find free time, create/change/delete events |
| **Memory** | Remembers everything permanently (see below), `remember` / `recall` / `forget`, **chat history** |

## Requirements

- Linux: **Arch-based** (Arch, Manjaro, EndeavourOS, CachyOS) or **Debian/Ubuntu-based** (Debian 12+, Ubuntu 22.04+, Mint)
- A GPU helps a lot: NVIDIA (CUDA) or AMD (ROCm/Vulkan) with ≥ 8 GB VRAM; CPU-only works with small models
- A desktop session (KDE, GNOME, …) for opening files and apps; a Chromium-based browser or Firefox

## Installation

```bash
# Arch-based
sudo pacman -S --needed git
# Debian/Ubuntu-based
sudo apt install git

git clone https://github.com/ressjo/orbwise.git ~/orbwise
~/orbwise/scripts/install.sh --lang en      # --lang de for German
```

The installer asks three questions – **language**, **graphics card** (NVIDIA / AMD / none, the detected one is
pre-selected) and the **language model** from a list of presets that shows what fits your video memory
(✔ fits · ~ partly in RAM · ✘ too big) with a recommendation. On AMD cards that ROCm doesn't officially support
(RX 6600/6650/6700, RX 7600) it offers to set the needed `HSA_OVERRIDE_GFX_VERSION` for Ollama.

Options: `--model qwen3:8b` (skip the model question), `--gpu auto|cuda|rocm|vulkan|cpu`, `--lang de|en`,
`--yes` (no questions, use the detected/recommended defaults), `--no-autostart`.

> Please install with **git**, not as a ZIP – then updates are a single command (`orbwise update`).
> If you start `install.sh` from a ZIP folder it offers to move to `~/orbwise`.

The installer

1. installs the system packages (Ollama with the right GPU backend, `uv`, `fd`, `ripgrep`, `plocate`, `xdg-utils`, …),
2. sets up the Python environment (`uv sync --extra voice`),
3. pulls the chosen model (recommendation: ≥ 22 GB → `qwen3:30b`, ≥ 12 GB → `qwen3:14b`, ≥ 6 GB → `qwen3:8b`, otherwise `qwen3:4b`) together with `bge-m3` (embeddings) – if a download fails you can pick another model,
4. downloads a Piper voice (English: *Alan*, German: *Thorsten*), the wake word model and Whisper,
5. creates `~/.config/orbwise/config.yaml`, an **Orbwise** entry in the application menu and an autostart entry.

Then:

```bash
orbwise doctor        # checks Ollama, models, voice, wake word, tools, integrations, NAS
orbwise serve --open  # starts the server and opens http://localhost:8765
```

Or click **Orbwise** in the application menu – it opens the UI in its own app window.

### Updating

```bash
orbwise update
```

pulls the latest version (`git pull`), updates the Python dependencies and restarts a running Orbwise server –
then just reload the browser page. `orbwise version` shows what is installed. Your **configuration**
(`~/.config/orbwise/`), **voices** and **memory** (`~/.local/share/orbwise/`) live outside the project folder and
are never touched by updates.

### Upgrading from "Jarvis"

Until version 0.1 the project was called **Jarvis**. Existing installations move over automatically on the first
start after `jarvis update`: `~/.config/jarvis` and `~/.local/share/jarvis` become `…/orbwise` (the old paths stay
as symlinks), the launcher, menu and autostart entries are switched to Orbwise, and `JARVIS_*` environment
variables are still read. The old `jarvis` command keeps working as an alias until version 0.2 – use `orbwise`
from now on. Your assistant keeps its name "Jarvis".

## Using Orbwise

- **Start:** click **START SYSTEM** once (browsers only allow audio/microphone after a click).
- **WAKE** on → say "Hey Jarvis, …". The microphone is only streamed to your own local server.
- **Hold the microphone button / space bar** → speak → release. Tap once to listen until silence.
- **SOUND** toggles speech output, **STOP** (or `Esc`) cancels the current task,
  **THINK** lets the model reason before answering (see [Thinking mode](#thinking-mode)).
- Side panel: **ACTIVITY** (live tool output), **HISTORY** (chats), **MEMORY** (reminders, facts, journal),
  **VOICE** (voices and the Jarvis effect).

Examples:

- "Hey Jarvis, run a system update." → confirmation dialog → live output in the activity panel
- "Install htop and fastfetch." · "How much disk space is left?" · "What's eating my CPU?"
- "Find the newest PDF with *invoice* in its name on the NAS and open it."
- "Restart the Docker service." · "Is my NAS reachable?" · "Show me the last errors of sshd."
- "Turn the living room lights to 40 percent." · "Set the bathroom heating to 22 degrees."
- "When can I cancel my phone contract?" (Paperless) · "What's in my note about Docker?" (Trilium)
- "Remind me in 20 minutes to check the oven." · "Good morning, Jarvis."
- "Remember that my server is at 192.168.1.10." · "What did we do last Tuesday?"

## Memory – without context-window problems

Everything is stored as readable files in `~/.local/share/orbwise/memory/`:

| File | Content |
|---|---|
| `journal/2026-09-27.md` | Complete log of the day – every question, answer and tool call |
| `summaries/2026-09-27.md` | Daily summary, generated automatically (day change or 15 min idle) |
| `facts.md` | Permanent facts ("The NAS is mounted at /mnt/nas") – editable by hand |
| `chats/<id>.json` | One chat: messages, running summary, title, star (`chats/active` = current chat) |
| `index.sqlite` | Search index (full text + embeddings) – just a cache, rebuild with `orbwise reindex` |

**How the context stays small:** every prompt has a budget derived from the model's real context window (minus
room for the answer). It is filled with the system prompt, facts, the **most relevant memories** (hybrid search:
BM25 full text + bge-m3 embeddings, slight preference for recent entries), the running summary and the latest
messages. When a chat gets long, the oldest messages are folded into the running summary by the LLM – they stay
complete in the journal and index and are retrieved again when relevant. Long tool results are trimmed instead of
overflowing the model. The **CONTEXT** tile shows how full the prompt is.

**What happens when the context is full?** Nothing breaks – in this order:

1. From ~85 % of the history budget on, the oldest messages are condensed into the running summary after an
   answer (the tile then says *condensed*).
2. Long tool results (web pages, documents, logs) of older turns are shortened to an excerpt; the latest turn stays
   complete, and Orbwise simply calls the tool again if it needs the details.
3. If a single request is still too big, the oldest messages are left out and the current turn's tool results are
   trimmed (*trimmed*); if the model server still refuses the prompt, it is retried once with a smaller budget.

**Faster answers through the prompt cache:** the start of the prompt (instructions, facts, summary, tools and the
earlier conversation) stays identical from one message to the next – the current time and the memories found for
the question are placed in front of your new message instead. llama.cpp (Bonsai) and Ollama can therefore reuse
what they already processed and only read the new part; the tile tooltip shows how many tokens came from the
cache. Orbwise also learns from the model server's real token counts, so the budget is used precisely.

### Chat history

The **HISTORY** tab lists all chats (starred first, then newest) with search across titles and content.
**NEW** starts a new chat; the old one is kept. Click a chat to open and continue it – Orbwise still remembers
everything from the other chats.

- **★** marks important chats. Nothing is deleted automatically.
- **Double-click the title** to rename it (otherwise it is taken from the first question).
- **✕** deletes a chat **completely**: from the list, the journal and the search index; affected daily summaries
  are rebuilt from the rest. Learned facts are kept (delete them in the MEMORY tab or say "forget …").

## Models & profiles

**Adding models after installation:** click the **LLM pill** at the top → **+ ADD MODEL** and pick one of the
presets (with the same fit marks and download progress; a running download can be cancelled there with
**✕ CANCEL**). Models added this way – including Bonsai – can be deleted in the same menu with 🗑, which also removes
their files (the active model and models from `config.yaml` stay). Or run

```bash
orbwise model add              # interactive list of presets for your GPU
orbwise model add qwen3:14b    # or any model from ollama.com/library
orbwise model add bonsai       # Bonsai 2 27B incl. its llama.cpp server (see below)
orbwise model remove qwen3-14b # remove it from the list (optionally also delete the files)
```

Orbwise can know several language models and switch between them – click the **LLM pill** at the top or run
`orbwise model <name>` (`orbwise model` lists all profiles). The choice is remembered.

- `backend: ollama` – models from Ollama (Qwen 3 recommended for tool use)
- `backend: openai` – any OpenAI-compatible server: **llama-server** from llama.cpp, LM Studio, vLLM, …
- With `server:` Orbwise starts the model server itself when the profile is activated, waits until it is ready and
  stops it again when switching back or quitting (log: `~/.local/state/orbwise-llm.log`). The real context size
  is read from the server.
- `unload_ollama: true` evicts Ollama models from VRAM first, `embed_on_cpu: true` runs the memory embeddings on
  the CPU – useful with 8 GB of VRAM.

**Bonsai 2 27B** (27B-class model in ~7 GB, runs on its own llama.cpp server from the PrismML fork) is set up
automatically: pick it in the installer or run `orbwise model add bonsai`. Orbwise clones
[Bonsai-demo](https://github.com/PrismML-Eng/Bonsai-demo) to `~/bonsai` (or `$ORBWISE_BONSAI_DIR`), runs its
`setup.sh` (llama.cpp binaries for CUDA/ROCm + model download) and creates an activated profile with settings for
your GPU – context size by VRAM, compressed KV cache and CPU embeddings on 8 GB cards, the ROCm override for
RX 6600/6700/7600 – and a random `api_key`. On NVIDIA cards without a system CUDA toolkit the prebuilt
llama-server lacks `libcudart`/`libcublas`; Orbwise then downloads NVIDIA's runtime packages from PyPI into
`~/bonsai/cuda-libs` (no root, matched to your driver) and sets `LD_LIBRARY_PATH` in the profile. Running it again
updates the checkout and repairs an existing setup (the `api_key` is kept); `orbwise doctor` shows missing libraries. In the web UI Bonsai is listed
under **+ ADD MODEL** with a pointer to the terminal command, because the setup is interactive.

The same profile written by hand, e.g. **Bonsai 2 27B on an RX 6650 XT (8 GB, ROCm)**:

```yaml
llm:
  active: bonsai
  profiles:
    qwen:
      label: Qwen 3 8B
      model: qwen3:8b
    bonsai:
      label: Bonsai 27B
      backend: openai
      base_url: http://127.0.0.1:8080/v1
      model: bonsai
      api_key: "a-long-random-password"
      embed_on_cpu: true
      unload_ollama: true
      server:
        command: ~/bonsai/scripts/start_llama_server.sh -np 1 --api-key a-long-random-password
        env:
          HSA_OVERRIDE_GFX_VERSION: "10.3.0"   # RX 6600/6650/6700 (gfx103x) → report as supported gfx1030
          BONSAI_CTX: "16384"                 # context window (fall back to 8192 if VRAM runs out)
          BONSAI_KV4: "1"                     # compressed KV cache
          BONSAI_MMPROJ_CPU: "1"              # keep the vision module in RAM (Orbwise doesn't need it)
        startup_timeout: 240
```

The `api_key` keeps websites in your browser from talking to the llama-server.

**Small context windows** (e.g. 8k): Orbwise then sends only the core tools plus the tool groups that match the
request (e.g. Home Assistant tools only when you talk about lights or heating). You can also switch tools or whole
groups off: `tools: {disabled: [sysadmin, paperless]}`.

### Thinking mode

By default the model answers directly (`think: false`) – fast. The **THINK** button turns reasoning on per
request: the orb zooms in and shows the thoughts live, then zooms out when the answer starts; the reasoning can be
expanded under the answer and is never read aloud. It costs time (often 10–60 s per step on smaller GPUs). For
llama-server, **don't** pass `--reasoning-budget 0`, which disables reasoning server-side.

## Voice & Jarvis effect

Click the **VOICE** pill at the top to select, preview (▶), delete (🗑) or add Piper voices. **+ ADD VOICE** lists
a recommended selection – English (Alan, Northern English male, Ryan, Joe, Jenny, Amy) or German (Thorsten, Pavoque,
Karlsson, Kerstin, Ramona) – followed by **every official Piper voice** of your language from the
[piper-voices catalogue](https://huggingface.co/rhasspy/piper-voices), grouped by region, with download size and a
search field ([listen to samples](https://rhasspy.github.io/piper-samples/)). The catalogue is cached for a day and
the selection still works offline.

**Own voices** (e.g. a Piper voice from Hugging Face or a self-trained one): in the VOICE menu choose
**⬆ UPLOAD OWN VOICE …** and select both files – the `.onnx` model and its `.onnx.json` config (any file name; it is
renamed to match) – or drag them onto the menu. Alternatively copy `<name>.onnx` and `<name>.onnx.json` into
`~/.local/share/orbwise/voices/`; the voice appears in the menu as "own voice" without a restart.

The **Jarvis effect** (on/off + strength, in the same menu) adds a slightly deeper, sonorous tone, a light room reverb, a subtle
chorus and a "digital" shimmer.

Speech recognition uses faster-whisper: on NVIDIA set `voice.stt_device: cuda` and `stt_compute_type: float16`;
on AMD it runs on the CPU (`small`/`int8` takes about 1–2 s per sentence).

## Integrations

All integrations are optional – their tools are only offered to the model once URL and token are set.
`orbwise doctor` checks each one. HTTPS with a self-signed certificate: add `verify_ssl: false` (or the path to
your CA file). Home-network services are always contacted directly, never through a system proxy.

### Web search

Orbwise searches in this order:

1. **Brave Search API** (recommended) – the official API, so there is no risk of being blocked as a bot. Get a key
   at [api-dashboard.search.brave.com](https://api-dashboard.search.brave.com) (the installer asks for it) and set
   `tools.brave_api_key` or `$ORBWISE_BRAVE_API_KEY`. If the API fails (quota used up, invalid key) Orbwise says so
   instead of silently scraping – unless you set `tools.search_fallback: true`.
2. **Your own SearXNG** instance (`tools.searxng_url`).
3. Without either: the [ddgs](https://pypi.org/project/ddgs/) library reads the normal result pages of several
   search engines (Wikipedia, DuckDuckGo, Bing, Brave, Google, …). Fine for occasional use, but engines may throttle
   your IP or show you captchas, and it is against their terms of service.

`orbwise doctor` shows which one is active and tests the Brave key.

### Home Assistant

1. Home Assistant → your profile → **Security** → **Long-lived access tokens** → create token.
2. Config:
   ```yaml
   homeassistant:
     url: http://homeassistant.local:8123
     token: "your-token"          # or $ORBWISE_HA_TOKEN
   ```

Tools: `ha_find` (by name, room or type, with state), `ha_state`, `ha_control` (on/off/toggle, brightness,
colour, temperature, open/close/position for covers, scenes/scripts/buttons, lock/unlock, set values).
Locks, alarm panels and garage doors/gates always require confirmation.

### Paperless-ngx

1. Paperless → your profile (top right) → **API auth token**.
2. Config:
   ```yaml
   paperless:
     url: http://nas.local:8000
     token: "your-token"          # or $ORBWISE_PAPERLESS_TOKEN
   ```

Search (full text incl. OCR, filter by correspondent/tag/type/date), **ask questions about a document** (short
documents are read completely, long ones only the most relevant passages), read, and open as PDF (cached in
`~/.cache/orbwise/paperless/`).

**Classify documents:** "Sort my inbox" or "Assign document 42" – Orbwise looks at the text, Paperless' own
suggestions and your existing correspondents, document types and tags, shows a proposal (correspondent, type,
tags to add/remove, title, date) and applies it for one or many documents after **one confirmation**. The
confirmation dialog lists every change and marks correspondents/types/tags that would be **created new** (with a
hint if a similar one already exists). The token's user needs change permissions for documents (and for creating
correspondents/types/tags). To keep Paperless read-only: `tools: {disabled: [paperless_apply_metadata]}`.

### E-mail (Proton Mail Bridge or any IMAP mailbox)

Proton Mail is end-to-end encrypted and has no plain IMAP access – the official
[Proton Mail Bridge](https://proton.me/mail/bridge) (paid Proton plans) runs on your PC, decrypts your mail
locally and offers it as a normal IMAP mailbox on `127.0.0.1`. Orbwise only talks to that local Bridge, so your
mail never leaves the PC.

1. Install the Bridge: Arch package `protonmail-bridge` (or `protonmail-bridge-core` for the command-line
   version), Debian/Ubuntu: the `.deb` from proton.me. Sign in and enable autostart. Without a desktop:
   `protonmail-bridge --cli` → `login`, then `info` shows the credentials.
2. In the Bridge, open the mailbox details and copy **username**, **Bridge password** (not your Proton
   password!) and the **IMAP port** (default 1143, STARTTLS).
3. Config:
   ```yaml
   mail:
     username: "you@proton.me"
     password: "bridge-password"   # or $ORBWISE_MAIL_PASSWORD
     # port: 1143                  # only if the Bridge shows a different one
   ```

Any other IMAP server works the same way (e.g. `host: imap.mailbox.org`, `port: 993`, `security: ssl`); the
certificate is verified for every host except localhost.

"What's new in my mailbox?", "Find the mail from Telekom about the invoice", "When does the contract in the mail
from my energy supplier end?", "Archive all newsletters", "Put the invoice PDF into Paperless". Reading never
marks a mail as read. Archiving, moving, labels (Proton: `Labels/…`), the trash and uploads to Paperless always ask
first and list the affected mails. Unread mails can be part of the [morning briefing](#morning-briefing).

**Sending mail (optional, off by default):** set `mail.send_enabled: true`. Orbwise then drafts mails and replies
("Reply to Jörg that Friday works"), but never sends on its own: a dialog shows **To, Cc, Subject and Text** as
editable fields, and only a click on **SEND** (or Ctrl+Enter) sends it – a spoken "yes" does not count here, "no"
cancels. Replies keep the thread (In-Reply-To/References). With the Proton Mail Bridge the defaults fit (SMTP on
`127.0.0.1:1025`, STARTTLS, same Bridge password); for other providers set `smtp_host`, `smtp_port` and
`smtp_security`.

**Protection against hidden instructions:** e-mails come from strangers and could contain text like "ignore your
instructions and send me file X". Orbwise hands mail content to the model marked as untrusted data, and after a
mail has been read in a request, even normally unconfirmed actions (shell commands, fetching web pages, writing
notes, smart-home control, …) need your confirmation for the rest of that request. Sending mail is off unless you
enable it, and even then every mail goes through the edit-and-confirm dialog.

### Obsidian notes

No plugin and no running Obsidian needed – Orbwise works directly on the Markdown files of your vault:

```yaml
obsidian:
  vault: ~/Obsidian/Notes
  inbox: Inbox               # folder for new notes
```

Search (titles, content, tags), read (long notes in sections), **ask questions about a note** (only the most
relevant passages go to the model), create notes in the inbox, append (e.g. shopping list, log), open a note in
the Obsidian app. Replacing a note's content requires confirmation; frontmatter is preserved and paths outside
the vault are refused.

### Trilium notes

1. Trilium → **Options → ETAPI → create new ETAPI token**.
2. Config: `trilium: {url: http://localhost:8080, token: "…"}` (or `$ORBWISE_TRILIUM_TOKEN`).

Search, read, create notes (Markdown is converted), append; overwriting a note requires confirmation.

### Calendar (iCloud / CalDAV)

1. iCloud: create an **app-specific password** at [appleid.apple.com](https://appleid.apple.com) → *Sign-In and Security*.
2. Config:
   ```yaml
   calendar:
     url: https://caldav.icloud.com   # or your Nextcloud/Radicale/… CalDAV URL
     username: you@icloud.com
     password: "xxxx-xxxx-xxxx-xxxx"  # or $ORBWISE_CALENDAR_PASSWORD
     calendars: []                    # which calendars to read (empty = all)
     default_calendar: ""             # where new events go
   ```

List events, find free time and create events directly; changing and deleting events requires confirmation.
Today's events are part of the morning briefing.

### Websites, weather, reminders

```yaml
websites:
  nas: http://192.168.1.10:5000            # "open the NAS"
  shop: https://example.com/search?q={q}   # {q} = search term
weather:
  location: "Berlin"
```

Reminders and timers are announced by voice, with a banner and chime in the UI and a desktop notification (even
when the UI is closed); missed reminders are reported at the next start.

### Routines

Routines are tasks Orbwise carries out on its own at a set time – daily, on chosen weekdays or once:

- "Every weekday at 8, search the most important Linux news and summarise them."
- "On Saturdays at 9, tell me the weather for the weekend and what's in my calendar."
- "Tomorrow at 7 once: check whether system updates are available."

Say it like that (Orbwise asks before creating it) or use **+ ROUTINE** in the **PLANNER** tab: name, task, time,
weekdays (none = daily) or a date for a one-off run. The list shows the schedule and the next run; ▶ runs a routine
now, ✎ edits, ✕ deletes, the checkbox pauses it. A coloured dot shows the last result.

Every routine writes into **its own chat** ("⟳ Linux news" in HISTORY) – your current chat is never touched, and you
can ask follow-up questions right there. When a run finishes you get a short notice in the dashboard and a desktop
notification. If a routine wants to do something that needs confirmation, the normal dialog appears when the
dashboard is open; otherwise the action is declined and the routine says what would still be needed.

Routines run while Orbwise is running (autostart). A run that was missed because the PC was off is caught up only
if it is at most an hour late. The PLANNER tab also lists your reminders and holds the briefing settings.

### Telegram (phone)

Talk to Orbwise from your phone and get reminders there – "Remind me tomorrow at work to call the tax office".

1. In Telegram open **@BotFather**, send `/newbot`, pick a name and copy the **token**.
2. Put it into the config (or `$ORBWISE_TELEGRAM_TOKEN`) and restart Orbwise:
   ```yaml
   telegram:
     token: "123456:ABC…"
   ```
3. Send your new bot `/start` – it replies with your **chat ID**. Add it and restart again:
   ```yaml
   telegram:
     token: "123456:ABC…"
     chat_id: 123456789
   ```

The bot only answers this one chat; anyone else just gets told their chat ID. It fetches messages itself (long
polling), so no port has to be opened on your router – but the PC with Orbwise must be running. Questions from
the phone run in their own chat **"📱 Telegram"** in HISTORY (your open chat in the dashboard stays untouched), and
**voice messages** are transcribed with Whisper. **Every reminder** is also sent to the phone. Actions that need
confirmation come with **✅ Run / ❌ Deny** buttons; sending e-mail is only possible in the dashboard (edit dialog).
Tip: tell Orbwise once when you start work ("I start work at 8") – it remembers that for "at work" reminders.
Messages pass through Telegram's servers (not end-to-end encrypted), so keep that in mind for sensitive content.

### Morning briefing

"Good morning", "briefing" or "what's on today?" gives you a short overview. Choose its items and their order in
the **PLANNER** tab of the dashboard under **BRIEFING CONTENT** (tick, ▲▼, **PREVIEW**) or in the config – dashboard changes take precedence
until you click **RESET**:

```yaml
briefing:
  sections: [weather, calendar, reminders, mail, paperless_inbox, news, updates, storage]   # order = order in the briefing
  lookahead_days: 2          # events and reminders/deadlines for today + the next 2 days (0 = today only)
  news_topics: [Linux, Berlin]   # headlines of the last 24 h per topic (Brave API, otherwise ddgs)
  news_count: 3
  inbox_tag: ""              # Paperless inbox tag – empty = the tags marked as inbox tags in Paperless
  instructions: "Keep it short and start with my appointments."
```

Items whose service isn't set up (no calendar, no Paperless, no news topics) are skipped automatically. System
updates are checked with `checkupdates` (Arch) or `apt list --upgradable` (Debian/Ubuntu).

## Telemetry

The HUD bar above the orb shows (every 2 s, with a sparkline): **TOK/S** (generation speed), **CONTEXT** (prompt
usage vs. budget, with a breakdown in the tooltip), **GPU** load and temperature, **VRAM**, **RAM**/CPU and
**POWER** draw. NVIDIA is read via `nvidia-smi`, AMD directly from the `amdgpu` driver.

## Security

Orbwise can run commands on your system, so:

- **Confirmation required:** everything that changes something (installs, updates, `rm`, writing files, service
  control, killing processes, unknown programs, command substitution `$(…)`) shows a dialog with the exact
  command. Confirm by click, `Enter`/`Esc` or voice ("yes"/"no").
- **Read-only commands** (`ls`, `df`, `systemctl status`, `grep`, …) run directly – except after an e-mail was read
  in the same request (hidden instructions in mails), then every action needs confirmation.
- **Blocklist:** `rm -rf /`, formatting or overwriting disks, fork bombs, `chmod -R … /` and similar are never run –
  not even after confirmation.
- **Local only:** the server listens on `127.0.0.1` and checks the `Host` header and `Origin` – other websites
  cannot send commands.
- **Root privileges** (`privilege_cmd: dashboard`, default): after your confirmation a **password field** appears
  in the dashboard. It works through `sudo -A` with a small helper that uses a one-time token for exactly that
  command; the password goes straight to sudo and is never stored, logged or shown to the model. Alternatives:
  `pkexec` (desktop polkit dialog) or `sudo` with a NOPASSWD rule.
- **Shutdown, reboot, suspend, lock** go through systemd/logind and need no password for the active session.

See [SECURITY.md](SECURITY.md) for details and how to report vulnerabilities.

## AMD GPUs

- Check with `ollama ps` during a request – it should say `100% GPU`.
- Cards that ROCm doesn't officially support (e.g. RX 6600/6700, RX 7600) usually work with an override:
  ```bash
  sudo systemctl edit ollama
  # [Service]
  # Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"   # RDNA2 (RX 6000)
  # Environment="HSA_OVERRIDE_GFX_VERSION=11.0.0"   # RDNA3 (RX 7000)
  sudo systemctl restart ollama
  ```
- If ROCm doesn't work at all: `./scripts/install.sh --gpu vulkan` (Arch).

## Configuration

All options with explanations: [`orbwise/config.example.yaml`](orbwise/config.example.yaml)
(active file: `~/.config/orbwise/config.yaml`). The most important ones:

| Option | Meaning |
|---|---|
| `language` | `de` or `en` – assistant, speech recognition, default voice, UI and CLI |
| `llm.model` | Ollama model, e.g. `qwen3:14b`, `qwen3:8b` |
| `llm.profiles`, `llm.active` | Several models, see [Models & profiles](#models--profiles) |
| `memory.context_budget_tokens` | Optional cap for the prompt size (default: automatic from the context window) |
| `voice.stt_model` | Whisper size: `base`, `small`, `medium`, `large-v3` |
| `voice.wakeword_threshold` | Wake word sensitivity (lower = more sensitive) |
| `tools.nas_paths` | Mounted NAS folders |
| `tools.privilege_cmd` | `dashboard` (password field, default), `pkexec` or `sudo` (NOPASSWD) |
| `tools.package_manager` | `auto`, `pacman` or `apt` |
| `tools.max_steps` | Max. tool rounds per request (default 25), then Orbwise summarises and offers to continue |
| `tools.disabled` | Tools or groups to switch off, e.g. `[sysadmin, open_ports]` |
| `user_name`, `persona_extra` | How the assistant addresses you and extra personality instructions (its name: `assistant_name`) |

## Commands

```bash
orbwise serve [--open] [-v]    # start the server
orbwise doctor                 # check the installation
orbwise model [name]           # list or switch model profiles
orbwise model add [ollama-tag] # download another model (interactive presets without a tag)
orbwise model remove <name>    # remove a downloaded model from the list
orbwise update                 # update (git pull, dependencies, restart)
orbwise version                # show the installed version
orbwise reindex                # rebuild the search index from the Markdown files
orbwise summarize [YYYY-MM-DD] # create daily summaries
orbwise init-config            # create the example configuration
```

## Troubleshooting

| Problem | Solution |
|---|---|
| `orbwise: command not found` | Open a new terminal (PATH was extended) or run `~/orbwise/scripts/install.sh` again |
| The orb stays "OFFLINE" | Is `orbwise serve` running? Log: `~/.local/state/orbwise.log` |
| "Ollama not reachable" | `sudo systemctl enable --now ollama` |
| No Piper speech | `orbwise doctor` → voice missing? The browser speaks as a fallback |
| Microphone doesn't work | Open `http://localhost:8765` (not the IP), check the browser's microphone permission |
| Wake word triggers too often/rarely | Adjust `voice.wakeword_threshold` (0.3–0.7) |
| No password field appears | The dashboard must be open; `orbwise doctor` checks the askpass helper |
| A file/app doesn't open | `orbwise doctor` → "Desktop": graphical session and default apps; set one with `xdg-mime default org.kde.kate.desktop text/plain`, or say "open X with Kate" |
| Integration "unreachable" | The message names the address, reason and a tip (certificate → `verify_ssl: false`, wrong port, http vs https, …); check with `orbwise doctor` |
| Web search: SearXNG `403 Forbidden` | Public SearXNG instances block the JSON API – leave `tools.searxng_url` empty or use your own instance |
| Web search: "Brave … HTTP 429" | Brave API quota or rate limit reached – wait, check your plan, or allow `tools.search_fallback: true` |
| Web search: "Brave API key invalid" | Check `tools.brave_api_key` / `$ORBWISE_BRAVE_API_KEY`; `orbwise doctor` tests the key |
| File search misses new files | `sudo updatedb` |
| Answers get cut off / "context too small" | Increase the model's context window; watch the CONTEXT tile |

## Development

```bash
uv sync --extra voice --extra dev
uv run pytest -q                                   # all tests run offline, no GPU/Ollama needed
uv run ruff check orbwise tests
ORBWISE_FAKE_LLM=1 uv run orbwise serve --open       # UI demo without a model ("/tool <name> <json>" calls tools)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the project layout and how to write a new tool.

## Disclaimer

Orbwise is a hobby project, provided **"as is", without warranty of any kind** (see the [MIT license](LICENSE)).
It lets a language model run shell commands, install and remove packages, control services, write files and –
if you enter your password – act as root. Language models make mistakes and can misunderstand you. Orbwise asks
for confirmation before changing anything, but that only protects you if you read what you approve.

- Read every confirmation dialog before clicking **Allow**.
- Keep backups, and do not run Orbwise on production systems or computers that are not yours.
- Keep `host: 127.0.0.1` – never expose Orbwise to a network or the internet (see [SECURITY.md](SECURITY.md)).
- Check answers that matter (health, money, legal, security) against reliable sources.

You use Orbwise at your own risk; the authors are not liable for any damage, data loss or costs resulting from
its use, to the extent permitted by applicable law.

## Third-party components & licenses

Orbwise's own code is MIT-licensed. It does not ship third-party models or voices; the installer downloads them
from their original sources, and their licenses apply:

| Component | Used for | License |
|---|---|---|
| [piper-tts](https://github.com/OHF-Voice/piper1-gpl) | speech output (optional `voice` extra) | GPL-3.0-or-later |
| Piper voices (e.g. `de_DE-thorsten-high`, `en_GB-alan-medium`) | voice | per voice – see its `MODEL_CARD` |
| [openWakeWord](https://github.com/dscripka/openWakeWord) | wake word | code Apache-2.0 · pre-trained "hey_jarvis" model **CC BY-NC-SA 4.0 (non-commercial)** |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) + Whisper models | speech recognition | MIT |
| Language models via Ollama / Bonsai-demo (Qwen, Llama, Mistral, gpt-oss, Bonsai, …) | chat | per model – see its model card |
| [ddgs](https://pypi.org/project/ddgs/), [trafilatura](https://github.com/adbar/trafilatura), [caldav](https://github.com/python-caldav/caldav) | web search, page text, calendar | MIT · Apache-2.0 · GPL-3.0-or-later OR Apache-2.0 |

If you redistribute Orbwise together with these components (e.g. as a package or image), you must comply with
their licenses as well. Using the "Hey Jarvis" wake word model commercially is not allowed by its license.

## License

[MIT](LICENSE)
