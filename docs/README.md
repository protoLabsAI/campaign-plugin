# Documentation

Start with the [README](../README.md) for the five-minute version. This directory has the rest.

| Doc | What's in it |
|---|---|
| [install.md](install.md) | Requirements, install, enable, the media setup banner |
| [overview.md](overview.md) | How it works: plans, takes, renders, cards, montages; what it won't do; the out-of-process browser and its guards |
| [usage.md](usage.md) | The plan → produce → review loop, the gallery, skills and the producer |
| [shot-scripts.md](shot-scripts.md) | The shot-script format, steps, frames, storage seeding, uploads, retiring a retake, directing a clip |
| [montages-and-branding.md](montages-and-branding.md) | Montages and storyboards, transitions, the protoLabs bookends, the brand intro and outro |
| [videos-from-a-request.md](videos-from-a-request.md) | `campaign_produce_video`: preflight, the background job, self-review gates, delivery |
| [media-and-backup.md](media-and-backup.md) | Where your media lives, retention, the protoContent archive, getting an archived asset back, collections |
| [limits.md](limits.md) | The documented platform limits renders are held to |
| [configuration.md](configuration.md) | Every config key, its default, live / reload, and which are operator-only |
| [tools.md](tools.md) | Every agent tool, the skills, the producer subagent's allowlist |
| [api.md](api.md) | Every HTTP route: the public view routes and the operator (bearer) data and archive routes |
| [development.md](development.md) | Running the suite, layout, the docs gate, releasing |

`tests/test_docs_reference.py` covers `api.md`, `tools.md`, `configuration.md`, the install ref
and the layout table: it fails if a route, an agent tool or a config key is added, renamed or
removed without its reference changing, or if a reference names one that no longer exists. That
is a COVERAGE guarantee — that the lists are honest and complete. What each entry *means* is still
a human's job.

**ADRs cited here and in the code are the HOST's**, unless named otherwise. This plugin has no
decision records of its own, so there is no `adr/` folder. The ADR numbers in the manifest and the
code comments (0018 plugin registration, 0019 agent self-config, 0026 console surfaces, 0027
plugin install, 0039 cross-plugin references and the event bus, 0071 declared capabilities, 0094
the managed Python runtime) live in
[protoAgent's `docs/adr/`](https://github.com/protoLabsAI/protoAgent/tree/main/docs/adr). The one
exception, protoContent's ADR 0009 (catalog PRs are merged by a human), is named as protoContent's
where it's cited.
