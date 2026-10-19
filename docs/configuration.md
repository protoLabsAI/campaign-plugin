# Configuration reference

Every key in the plugin's `campaign:` config section, its default, and when a change takes
effect. `tests/test_docs_reference.py` fails if a key is added to or removed from
`protoagent.plugin.yaml`'s `config:` without this table changing, and if the **Operator-only**
column disagrees with the manifest's `spawns: true` / `secrets:`.

## How a change takes effect

| Applies | Meaning |
|---|---|
| `live` | read on every use (the library surface's tick, the archive routes, `campaign_backup`, `campaign_prune`), so a Settings save takes effect with no restart |
| `reload` | read when the plugin registers, so it takes effect when the plugin reloads (a Settings save reloads it) |

Every key has a field in **Settings ▸ Plugins ▸ Campaign Studio**, in the group named below.

**Operator-only** (`yes`) keys carry `spawns: true` in the manifest, so the agent's own
`set_config` tool refuses to write them: they name a program the plugin runs, a local file it
reads, which files may leave the machine, whether files may be deleted, or where and as whom
media is sent. `secret` keys live in `secrets.yaml` (0600), are redacted in the UI and are never
logged.

## Minimal config

```yaml
# config/langgraph-config.yaml
plugins:
  enabled: [campaign]

campaign:
  brand_kit_path: /path/to/campaign-plugin/brands/protolabs/brand-kit.yaml   # optional: the protoLabs look + bookends
  retention_mode: report        # the default: a daily dry run that deletes nothing
```

Everything else has a working default; the media tools say what is missing.

## Keys

| Key | Default | Applies | Settings group | Operator-only | What it does |
|---|---|---|---|---|---|
| `data_dir` | `""` | `reload` | Campaign Studio |  | Where `campaign.db` and every take, render and card live. Blank = the host's per-instance plugin store. See [Where your media lives](media-and-backup.md#where-your-media-lives). |
| `ffmpeg_path` | `""` | `reload` | Campaign Studio | yes | Absolute path to ffmpeg when it isn't on `PATH`; ffprobe is looked up beside it. |
| `interpreter` | `""` | `reload` | Campaign Studio | yes | The Python (with playwright) that runs the browser worker. Blank = the desktop app's managed runtime, else the agent's own Python on a source install. Strict: a bad value is reported, never silently replaced. |
| `brand_kit_path` | `""` | `reload` | Brand look |  | A Social Studio brand-kit YAML to read card colours, fonts and logo from (read-only). Blank auto-detects. Point it at `brands/protolabs/brand-kit.yaml` for the protoLabs look and its default bookends. |
| `brand_name` | `""` | `reload` | Brand look |  | Fallback brand name for cards when no kit sets one. |
| `brand_colors` | `""` | `reload` | Brand look |  | Fallback card colours, e.g. `bg=#0d0f14, fg=#f4f5f7, accent=#7c5cff, muted=#9aa0ac`. |
| `brand_fonts` | `""` | `reload` | Brand look |  | Fallback installed font families, e.g. `heading=Inter, body=Inter`. |
| `brand_logo` | `""` | `reload` | Brand look |  | Fallback logo image path (png, svg or jpg). |
| `limit_overrides` | `""` | `reload` | Campaign Studio |  | YAML: `{limit_id: {max_bytes: …, source: …, as_of: …}}` to override or add [hard-limit](limits.md) rows. |
| `producer_model` | `""` | `reload` | Crew models |  | Model override for the `campaign_producer` subagent. Blank = the host's aux-model routing. |
| `bearer_envs` | `""` | `reload` | Campaign Studio |  | Extra env-var NAMES a shot script may use as `auth.bearer_env`, besides any `CAMPAIGN_*`. The host's own operator/fleet token is always refused. |
| `upload_dirs` | `""` | `reload` | Campaign Studio | yes | Folders an `upload` step may read files from (comma/newline separated). Blank refuses every upload. See [File uploads](shot-scripts.md#a-shot-script). |
| `outro` | `""` | `reload` | Brand look | yes | YAML: the brand outro (`caption_hold_s`, `min_caption_s`, `caption_fade_s`, `beat_s`, `transition`, `bumper_fade_ms` or `into_cta`, `bumper`, `cta`). See [The brand outro](montages-and-branding.md#the-brand-outro). |
| `intro` | `""` | `reload` | Brand look | yes | YAML: the brand intro (`bumper`, `card`, `bumper_fade_ms` or `transition`, `into_content`, `max_s`). See [The brand intro](montages-and-branding.md#the-brand-intro). |
| `retention_mode` | `report` | `live` | Library | yes | `report` (daily dry run, deletes nothing) · `prune` (delete per policy) · `disabled`. See [Retention](media-and-backup.md#retention). |
| `archive_enabled` | `false` | `live` | Library | yes | Back approved media up to protoContent. Needs a credential (app or token). See [Archive to protoContent](media-and-backup.md#archive-to-protocontent). |
| `archive_repo` | `"protoLabsAI/protoContent"` | `live` | Library | yes | `owner/name` of the archive repo. |
| `archive_catalog_dir` | `"specs/media/catalog"` | `live` | Library | yes | Folder for the per-campaign catalog JSON in the archive repo. |
| `archive_app_id` | `0` | `live` | Library | yes | The GitHub App the backup acts as (protoquinn = `3282126`); 0 = off. When set, the app wins over `archive_token`. |
| `archive_app_installation_id` | `0` | `live` | Library | yes | The app's installation (protoquinn on protoLabsAI = `121562683`); 0 = look it up from the repo. |
| `archive_app_private_key` | `""` | `live` | Library | secret | The app's PEM private key. A one-line paste is re-wrapped. Stored in `secrets.yaml`. |
| `archive_app_private_key_path` | `""` | `live` | Library | yes | …or an absolute path to the `.pem` (chmod 600), used only when the secret is blank. |
| `archive_token` | `""` | `live` | Library | secret | A fine-grained PAT on the archive repo only (Contents RW + Pull requests RW), used only when no app id is set. Stored in `secrets.yaml`. |

## In one paragraph

`data_dir` (blank = the host's per-instance plugin store), `ffmpeg_path`, `interpreter` (a
Python with playwright for the browser worker; blank = managed runtime, else the agent's own —
named so core's agent self-config fence refuses agent writes to it), `brand_kit_path`
(a Social Studio kit to read; blank auto-detects), `brand_name` / `brand_colors` /
`brand_fonts` / `brand_logo` (fallbacks), `limit_overrides`, `bearer_envs` (extra env-var
names a shot script may use as its bearer), `upload_dirs` (folders an `upload` step may read
files from; blank refuses uploads; operator-only), `outro` (the brand outro — see [The brand
outro](montages-and-branding.md#the-brand-outro); operator-only), `intro` (the brand intro — see [The brand intro](montages-and-branding.md#the-brand-intro); operator-only),
`producer_model`, `retention_mode` (`report` | `prune` | `disabled` — see [Retention](media-and-backup.md#retention);
operator-only), `archive_enabled` / `archive_repo` / `archive_catalog_dir` /
`archive_app_id` / `archive_app_installation_id` / `archive_app_private_key` /
`archive_app_private_key_path` / `archive_token` (see [Archive to protoContent](media-and-backup.md#archive-to-protocontent); operator-only,
the key and the token secrets).
