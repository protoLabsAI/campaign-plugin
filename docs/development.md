# Development

Contributor notes: running the suite, the file layout, the docs gate, and releasing.

## Running the suite

```bash
uv sync                                  # dev env incl. playwright
uv run python -m playwright install chromium   # only for the integration tests
uv run pytest -q                         # unit + integration (integration skips without Chromium/ffmpeg)
uv run ruff check . && uv run ruff format --check .
```

The suite is host-free: it bootstraps the plugin as a synthetic package (`tests/conftest.py`),
runs the REAL worker code in-process against a fake `sync_playwright()` (the job still
round-trips through JSON), fakes ffmpeg at its runner, and exercises the subprocess machinery
for real (interpreter probe, stdin-only secrets, bounded output, kill of an overrunning process
tree). The montage tests check validation, framing and the exact filtergraph with no ffmpeg,
and a real-ffmpeg montage of synthetic solid-colour clips asserts the duration, resolution,
codec/faststart, and that frames mid-transition carry the next clip's theme colour. The
`integration` tests drive real Chromium through the real worker process: a take →
render, the fence + bearer scoping inside the worker, a card that can't phone home, a hung
browser that gets killed, and a shoot + card from a host process that is forbidden to import
playwright. CI runs both: the unit job, and an integration job that installs Chromium + ffmpeg.

**Release ritual:** bump `version` in `protoagent.plugin.yaml`, `pyproject.toml` and
`__init__.__version__` together (a test enforces lockstep), bump the install ref in
`README.md` and `docs/install.md` to the new tag (`tests/test_docs_reference.py` enforces it),
`uv lock`, and land a
`chore: release vX.Y.Z` commit on main — `.github/workflows/release.yml` tags and publishes via
`protoLabsAI/release-tools`. Never `gh release create` by hand.

## Layout

| Path | What it is |
|---|---|
| `__init__.py` | `register()`: config, setup banner, tools, the producer, the three routers, the library surface |
| `tools.py` | Every agent tool (`campaign_*`) — see [tools.md](tools.md) |
| `api.py` | The view router (public gallery + signed open link) and the gated data router — see [api.md](api.md) |
| `archive_api.py` | The archive's gated routes, at their own prefix |
| `view.py` | The gallery page (one HTML string) |
| `store.py` | SQLite: campaigns, lanes, assets, milestones, decisions, shot scripts |
| `paths.py` | The data dir and media root |
| `shotscript.py` | Shot-script parsing and validation |
| `shoot.py` | A validated script → a recorded take, via the worker |
| `worker/pw_worker.py` | The out-of-process Playwright worker — every browser runs here |
| `interpreter.py` | Which Python runs the worker, and running it safely |
| `deps.py` | Probing playwright, Chromium and ffmpeg; the setup banner |
| `capture.py` | `capture: device` frames → a constant-frame-rate H.264 take |
| `render.py` | A take → mp4 / gif / poster under a hard limit |
| `cards.py` | Branded HTML templates → PNG |
| `templates/` | The card templates |
| `brand.py` | The brand look: kit file, settings fallbacks, defaults |
| `brands/protolabs/` | The protoLabs brand snapshot: kit, DS tokens, fonts, splash bumper |
| `scripts/gen_brand_protolabs.py` | Regenerates `brands/protolabs/` from `@protolabsai/design` |
| `montage.py` | Montages and storyboards |
| `transitions.py` | The signature `edge_sweep` transition, frame-exact |
| `transitions.json` | The transition contract shared with the console |
| `intro.py` | The brand intro preset |
| `outro.py` | The brand outro preset |
| `produce.py` | `campaign_produce_video` background jobs |
| `look.py` | `campaign_view`: stills and clip frames as images the model sees |
| `limits.py` | The hard-limit table |
| `library.py` | Collections: move / copy with lineage |
| `retention.py` | Retention plan and prune |
| `archive.py` | The protoContent backup: release assets + catalog PR |
| `surface.py` | The library surface: retention + archive ticks |
| `openlink.py` | Signed links for "Open in new tab" |
| `subagents.py` | The `campaign_producer` subagent |
| `sandbox.py` | `campaign_sandbox_reset`: resolve + guard a sandbox console, then clear it via the worker |
| `threads.py` | Run a blocking call on a fresh worker thread |
| `skills/` | The five skills |
| `docs/assets/` | Screenshots used by the README and docs (WebP) |
| `tests/` | The host-free suite (`conftest.py` bootstraps the plugin as a package) |

`tests/test_docs_reference.py` fails if this table names a path that doesn't exist.

## The docs gate

`tests/test_docs_reference.py` keeps the reference docs honest, in both directions:

- every route the three routers register is a row in [api.md](api.md), and api.md names no
  route that's gone;
- every `@tool` in `tools.py` is in [tools.md](tools.md), and its table names no tool that's gone;
- every key in the manifest's `config:` is a row in [configuration.md](configuration.md), the
  table names no key that's gone, and its Operator-only column matches `spawns:` / `secrets:`;
- the install ref in `README.md` and `install.md` is the manifest's version, and every
  `python -m server plugin <cmd>` they show is a real subcommand;
- this Layout table names only paths that exist.

It checks coverage, not prose: what an entry *means* is still a reviewer's job.
