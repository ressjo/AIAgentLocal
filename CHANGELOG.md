# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] – 2026-09-28

First public release.

### Assistant
- Local LLM via **Ollama** (Qwen 3 recommended) or any **OpenAI-compatible server** (llama.cpp `llama-server`,
  e.g. Bonsai 27B); switchable **model profiles**, Jarvis can start/stop the model server itself.
- Native tool calling with a **confirmation step** for anything that changes the system, a blocklist for
  destructive commands and root access through a **password field in the dashboard** (`sudo -A`).
- **Thinking mode** button: the model's reasoning is shown live inside the orb.
- Step limit with loop detection and a "continue" summary instead of a hard stop.
- German and English (`language: de|en`) – prompt, voice, speech recognition, UI and CLI.

### Voice
- Wake word "Hey Jarvis" (openWakeWord), push-to-talk, faster-whisper speech recognition.
- Piper text-to-speech with installable German and English voices and an optional "Jarvis" voice effect.

### Memory
- Persistent memory as readable Markdown files (daily journal, daily summaries, facts) plus a hybrid
  full-text/embedding index – no context-window overflow.
- **Chat history**: several chats, continue old ones, star them, delete them completely (including memory).
- Context budget follows the model's real context window; long tool results are trimmed instead of failing.

### Tools
- System: updates, install/remove/search packages (**pacman/AUR and apt**), system info, shell commands.
- System & network: processes, systemd services and logs, network info, ping, open ports, port check,
  disk usage and clean-up, power (shutdown, reboot, suspend, lock).
- Files & apps: find and open files (also on a NAS), read/write text files, launch applications, websites.
- Web search (DuckDuckGo or your own SearXNG) and page fetching, weather (Open-Meteo), reminders and timers,
  morning briefing.
- Integrations: **Home Assistant**, **Paperless-ngx** (search, ask, open documents), **Trilium** notes,
  **CalDAV/iCloud** calendar.
- Small context windows get only the tool groups that match the request; `tools.disabled` switches tools off.

### Web UI
- Animated neural-network orb, live telemetry (tokens/s, context, GPU, VRAM, RAM, power), activity log,
  memory browser, voice settings – optimised for smooth rendering in Firefox.

### Installation
- One-line installer for Arch-based and Debian/Ubuntu-based systems, `jarvis doctor`, `jarvis update`.
