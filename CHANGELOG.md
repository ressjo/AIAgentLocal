# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] – 2026-09-28

First public release.

> The project was developed as **Jarvis** and renamed to **Orbwise** for this release. The assistant's default
> persona is still "Jarvis" ("Hey Jarvis"). Earlier installations are migrated automatically; the old `jarvis`
> command and `JARVIS_*` environment variables keep working as aliases and will be removed in 0.2.

### Assistant
- Local LLM via **Ollama** (Qwen 3 recommended) or any **OpenAI-compatible server** (llama.cpp `llama-server`,
  e.g. Bonsai 27B); switchable **model profiles**, Orbwise can start/stop the model server itself.
- Native tool calling with a **confirmation step** for anything that changes the system, a blocklist for
  destructive commands and root access through a **password field in the dashboard** (`sudo -A`).
- **Thinking mode** button: the model's reasoning is shown live inside the orb.
- Step limit with loop detection and a "continue" summary instead of a hard stop.
- German and English (`language: de|en`) – prompt, voice, speech recognition, UI and CLI.

### Voice
- Wake word "Hey Jarvis" (openWakeWord), push-to-talk, faster-whisper speech recognition.
- Piper text-to-speech with installable German and English voices and an optional "Jarvis" voice effect.
- Voices are managed in the menu of the **VOICE** pill (like the models): select, preview, delete, add from the
  catalogue, plus the Jarvis effect – the separate VOICE tab is gone. "+ ADD VOICE" offers every official Piper
  voice of the language (catalogue cached for a day, search field, grouped by region, download size); own Piper
  voices (e.g. from Hugging Face) can be uploaded there (.onnx + .onnx.json, file dialog or drag & drop). The space bar no longer starts push-to-talk
  while typing in any input field (e.g. the planner).
- Commands are not read aloud: a confirmation only asks "Do you want to run the following command?" (the command is
  shown in the dialog), and command-like inline code in answers is skipped when speaking.

### Memory
- Persistent memory as readable Markdown files (daily journal, daily summaries, facts) plus a hybrid
  full-text/embedding index – no context-window overflow.
- **Chat history**: several chats, continue old ones, star them, delete them completely (including memory).
- Context budget follows the model's real context window; long tool results are trimmed instead of failing.
- The search index repairs itself when damaged ("database disk image is malformed"): the full-text index is rebuilt,
  a broken file is set aside and refilled from the Markdown memory files in the background; deleting chats never
  fails because of it, `orbwise reindex` works on a broken file and `orbwise doctor` checks the index.
- Cache-friendly prompts: time and retrieved memories are placed in front of the new message instead of the system
  prompt, so llama.cpp/Ollama reuse their prompt cache; old long tool results are aged to excerpts, condensing starts
  at ~85 % of the budget, and the token estimate calibrates itself from the server's real counts.

### Tools
- System: updates, install/remove/search packages (**pacman/AUR and apt**), system info, shell commands.
- System & network: processes, systemd services and logs, network info, ping, open ports, port check,
  disk usage and clean-up, power (shutdown, reboot, suspend, lock).
- Files & apps: find and open files (also on a NAS), read/write text files, launch applications, websites.
- Web search (Brave Search API, your own SearXNG or ddgs) and page fetching, weather (Open-Meteo), reminders
  and timers, a morning briefing with selectable items and order (weather, events and deadlines of the next
  days, Paperless inbox, news, updates, storage) – configurable in the config or the dashboard's PLANNER tab.
- Integrations: **Home Assistant**, **Paperless-ngx** (search, ask, open documents; suggest and apply
  correspondent, type, tags, title and date – also for many documents at once, after one confirmation),
  **Obsidian** vaults, **Trilium** notes and a **CalDAV/iCloud** calendar.
- **Routines**: tasks run automatically at set times (daily, weekdays or once), e.g. a web search every morning –
  created by voice or in the new **PLANNER** tab (which also holds reminders and the briefing settings); each
  routine writes into its own chat, confirmations are asked in the dashboard or declined when nobody is there.
- **Telegram bot** (optional): chat with Orbwise from the phone (text or voice messages, own "📱 Telegram" chat),
  every reminder is also sent to the phone, actions that need confirmation get ✅/❌ buttons; only your own chat ID
  is served, no open port needed (long polling). Files in both directions: PDFs, documents and photos sent to the
  bot are saved (and can go straight into Paperless), and Orbwise can send local files or Paperless documents to the
  phone (never keys or password stores). `/stop` (or "stop") from the phone cancels what is running – there and on
  the PC. The bot token is kept out of logs, error messages and the dashboard; once `telegram.chat_id` is set,
  messages from other chats are ignored silently.
- **Secret files are protected**: `read_file`, sending to the phone and uploading to Paperless refuse SSH/GPG keys,
  password stores, browser logins, cloud credentials and Orbwise's own config (also through symlinks); shell
  commands that read such files, `printenv`/`env`, token variables or Wi-Fi passwords (`nmcli -s`) need confirmation.
- Orbwise refuses to listen on a non-local address unless `allow_remote: true` is set (the dashboard has no login).
- Confirmation reasons are shown in English when `language: en`.
- README: notice that Orbwise was built with AI help, extended disclaimer, complete feature list, new screenshots
  (`scripts/screenshots.mjs` regenerates them); `config.example.yaml` now lists every option (secrets left empty).
- **E-mail** via IMAP – **Proton Mail through the Proton Mail Bridge** or any other mailbox: list unread mails,
  search, read (without marking as read), ask about a mail, archive/move/label/trash and PDF attachments to
  Paperless after confirmation, unread mails in the briefing. Mail content is passed to the model as untrusted
  data; after reading a mail, all further actions in that request need confirmation. Optional **sending**
  (`mail.send_enabled`, SMTP – Proton Bridge defaults): Orbwise drafts mails and replies, a dialog shows To, Cc,
  Subject and Text as editable fields and only a click on SEND sends; replies keep the thread.
- Small context windows get only the tool groups that match the request; `tools.disabled` switches tools off.

### Web UI
- Animated neural-network orb, live telemetry (tokens/s, context, GPU, VRAM, RAM, power), activity log,
  memory browser, voice settings – optimised for smooth rendering in Firefox.
- Orb: start-up sequence (rings assemble, network ignites), shock-wave impulses on state changes (wake word, answer,
  tool, error), the voice shapes the main ring while speaking/listening, particles flow in/out, and satellites on
  the ring show which tool or routine is running (larger, visible for at least 1.5 s; a tool call no longer ends the
  thinking zoom); the side panel is narrower, the orb wider.
  Satellites live on their own layer that does not zoom, so in thinking mode they circle the thought view at full
  brightness; a light beam shoots from the core to each tool and the result flows back (green ok, red error).
  While thinking, impulses run from the core outwards through the network – faster with every token, calmer when
  the stream stalls. A thin context ring shows how full the context is (orange from 85 %, flashes when condensed).
  Actions get symbols: a terminal for shell/packages/system tools, a cloud for web search, an envelope for mail and a
  document with a scan bar for Paperless, with light dots flowing
  from the core into the symbol; memory actions make the neurons in the core glow gold instead. HUD details: message glide-in, panel corners light up on
  activity and a copy button on code blocks – all within the same frame budget.
- The status returns to "ready" as soon as the answer is complete (condensing runs silently afterwards), after the
  model has loaded or failed to load, and after reconnecting – no more stuck "thinking". The dashboard switches to
  "ready" by itself when the answer is complete, a cancelled transcription no longer blocks the microphone, and the
  browser always revalidates the UI files. The page loads its scripts with a content version, so after an update the
  browser can no longer mix new and cached old files (which hid tools in chat, activity and orb).

### Installation
- Installer for Arch-based and Debian/Ubuntu-based systems that asks for the language, the GPU (NVIDIA/AMD/CPU,
  pre-selected from detection, ROCm override for RX 6600/6700/7600) and the model from a VRAM-aware preset list.
- Add models later with `orbwise model add` or **+ ADD MODEL** in the web UI (download progress, fit marks); running
  downloads can be cancelled in the model menu, and added models (including Bonsai) can be deleted there with 🗑 –
  the model files are removed and the freed size is shown.
- **Bonsai 2 27B** is set up automatically when chosen in the installer or with `orbwise model add bonsai`
  (Bonsai-demo checkout, llama.cpp binaries, model, GPU-specific server profile). On NVIDIA the CUDA runtime
  libraries are fetched automatically when the system has none.
- Web search via the official **Brave Search API** (key asked for by the installer) – no scraping of result
  pages and no bot blocking; SearXNG and ddgs remain as alternatives.
- `orbwise doctor`, `orbwise update`.

### Project
- MIT license, security policy, disclaimer and an overview of third-party components and their licenses.
