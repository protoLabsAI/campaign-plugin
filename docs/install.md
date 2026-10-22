# Requirements and install

Get the plugin running. For the keys see the [configuration reference](configuration.md).

## Requirements

- **protoAgent ≥ 0.188.0** (`min_protoagent_version` in `protoagent.plugin.yaml`): the setup
  banner's Install Chromium button (`register_setup_step`), the gallery's DS kit,
  `sdk.plugin_store` and the `install_deps` action.
- **Python 3.11+** (the host's).
- **playwright** (declared optional, `scope: runtime`) and its headless **Chromium**, for takes
  and cards. Never imported into the agent's own process: see
  [The out-of-process browser](overview.md#the-out-of-process-browser).
- **ffmpeg** (with ffprobe beside it), a system binary, for renders and montages.
- **PyJWT[crypto]** (declared optional, `scope: host`) only for the archive's GitHub App auth.
  protoAgent already ships it.

Planning needs none of the media pieces; the media tools say exactly what's missing.

## Install, enable, set up media

1. **Install** (pin a tag): Settings ▸ Plugins ▸ Install from URL →
   `https://github.com/protoLabsAI/campaign-plugin`, ref `v0.8.1`. Or
   `python -m server plugin install https://github.com/protoLabsAI/campaign-plugin --ref v0.8.1`.
2. **Enable** it (`plugins.enabled: [campaign]`). It ships disabled.
3. **Set up media** — the setup banner walks you through it:
   - **Desktop app only:** provision the **Python runtime** first (Settings ▸ Tools, ~35 MB) —
     that's where playwright runs. The banner says so, with a button to get there.
   - **Install dependencies** installs the `playwright` package — into the managed runtime on
     the desktop app, into the agent's venv on a source install.
   - **Install Chromium** downloads Playwright's headless Chromium (~150 MB) with that same
     Python. Only ever from that button — never as a side effect of a tool call.
   - **Check again** re-probes after a fix made elsewhere (any media tool call does too).
   - **ffmpeg** is a system binary: `brew install ffmpeg` / `sudo apt install ffmpeg` /
     `winget install Gyan.FFmpeg`, or set **ffmpeg path**.
   Planning works without any of these; the media tools say exactly what's missing.
4. Ask the agent: *"Plan a launch campaign for <product> at <url>"* — then *"produce the
   agent-owned assets"* (it hands that to the `campaign_producer` subagent), then open the
   **Campaign Studio** rail view to approve or reject what came back.
