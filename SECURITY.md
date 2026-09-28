# Security

Jarvis can run commands on your computer, so its safety model matters.

## How Jarvis protects your system

- **Local only.** The server binds to `127.0.0.1`, checks the `Host` header (DNS rebinding) and the `Origin`
  of WebSocket and write requests (CSRF), so other websites cannot send commands.
- **Confirmation.** Every tool that changes the system (shell commands that modify something, package
  installs, service control, killing processes, file writes, locks/alarms in Home Assistant, …) is shown in
  the dashboard and must be approved. Read-only commands run directly.
- **Blocklist.** Obviously destructive commands (e.g. `rm -rf /`, formatting or overwriting disks, fork bombs,
  `chmod -R` on `/`) are never executed; risky ones such as piping a download into a shell or partitioning
  tools are flagged with an extra warning in the confirmation dialog.
- **Root access** goes through `sudo -A` with a one-time token per command; the password is typed into the
  dashboard, handed straight to sudo and never stored, logged or shown to the language model.
- **Home network services** (Trilium, Paperless, Home Assistant) are contacted directly, never through a proxy;
  tokens stay in your local config file.

Please keep `host: 127.0.0.1`. Exposing Jarvis to a network gives anyone on that network a way to ask it to
run commands.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's private
[security advisory](https://github.com/ressjo/AIAgentLocal/security/advisories/new) form instead. You will get
an answer as soon as possible; fixes are released as a new version and mentioned in the changelog.
