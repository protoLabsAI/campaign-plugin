"""Manifest coherence, register() wiring, the gallery's four rules, and path containment."""

from __future__ import annotations

import os
import re
import sys
import tomllib
import types
from pathlib import Path

import campaign
import pytest
import yaml
from campaign import api, paths, store, view
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text(encoding="utf-8"))

TOOLS = {
    "campaign_create",
    "campaign_update",
    "campaign_list",
    "campaign_get",
    "campaign_lane",
    "campaign_asset_add",
    "campaign_asset_update",
    "campaign_assets",
    "campaign_milestone",
    "campaign_decision",
    "campaign_status",
    "campaign_limits",
    "campaign_setup",
    "campaign_script_save",
    "campaign_shoot",
    "campaign_render",
    "campaign_card",
    "campaign_view",
    "campaign_montage",
    "campaign_storyboard",
}


# ── manifest ──────────────────────────────────────────────────────────────────
def test_identity_and_trust_defaults():
    assert MANIFEST["id"] == "campaign"
    assert MANIFEST["config_section"] == "campaign"
    assert MANIFEST["enabled"] is False, "install is not consent; the operator enables it"
    assert MANIFEST["repository"] == "https://github.com/protoLabsAI/campaign-plugin"


def test_version_is_in_lockstep_across_the_three_places_it_appears():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert MANIFEST["version"] == pyproject["project"]["version"] == campaign.__version__


def test_floor_is_a_host_with_register_setup_step():
    major, minor, _ = (int(x) for x in MANIFEST["min_protoagent_version"].split("."))
    assert (major, minor) >= (0, 165), "register_setup_step shipped in protoAgent v0.165.0"


def test_every_config_key_has_a_settings_row_and_vice_versa():
    assert set(MANIFEST["config"]) == {s["key"] for s in MANIFEST["settings"]}


def test_settings_that_name_a_program_to_run_are_marked_spawns():
    """The agent's set_config may tune this plugin, but must never point it at a binary it
    then runs (protoAgent ADR 0019 §3b). `ffmpeg_path` has no name the core fence can see,
    so the marker is the only thing fencing it; data paths must stay unmarked."""
    spawns = {s["key"] for s in MANIFEST["settings"] if s.get("spawns") is True}
    assert spawns == {"ffmpeg_path", "interpreter"}


def test_playwright_is_declared_runtime_scoped_and_optional():
    # NOT host: the host process never imports playwright (a frozen app can't install it), so
    # install-deps must route it to the managed runtime the worker runs on.
    (entry,) = MANIFEST["requires_pip"]
    assert entry["pkg"].startswith("playwright") and entry["scope"] == "runtime" and entry["optional"] is True


def test_the_host_side_never_imports_playwright():
    host_sources = {p.name: p.read_text(encoding="utf-8") for p in ROOT.glob("*.py")}
    for name, src in host_sources.items():
        assert not re.search(r"^\s*(from|import)\s+playwright", src, re.M), f"{name} imports playwright in the host"
    worker = (ROOT / "worker" / "pw_worker.py").read_text(encoding="utf-8")
    top = [ln for ln in worker.splitlines() if re.match(r"(from|import)\s", ln)]
    assert not any("playwright" in ln for ln in top), "the worker imports playwright only inside functions"


def test_the_interpreter_setting_is_behind_cores_self_config_fence():
    # core refuses agent writes to any leaf key named interpreter/executable/command/...; a key
    # called python_path would let an agent point the worker at a program of its choosing.
    assert "interpreter" in MANIFEST["config"] and "python_path" not in MANIFEST["config"]


def test_declared_capabilities_match_reality():
    # The recording browser navigates to whatever a shot script names — like agent_browser,
    # that is "*", not "no network". (v0.1.0 declared [] and under-stated the blast radius.)
    assert MANIFEST["capabilities"] == {"network": ["*"], "filesystem": "scoped"}
    sources = "\n".join(p.read_text(encoding="utf-8") for p in [*ROOT.glob("*.py"), *ROOT.glob("worker/*.py")])
    for client in ("import httpx", "import requests", "urllib.request", "aiohttp"):
        assert client not in sources, f"the plugin's Python makes no outbound calls of its own ({client})"


def test_no_cross_plugin_imports():
    sources = "\n".join(p.read_text(encoding="utf-8") for p in [*ROOT.glob("*.py"), *ROOT.glob("worker/*.py")])
    assert not re.search(r"^\s*(from|import)\s+(social|plugins\.|agent_browser|artifact)", sources, re.M)


# ── register() ────────────────────────────────────────────────────────────────
def test_register_contributes_tools_routers_and_the_setup_step(registry):
    campaign.register(registry)
    assert set(registry.tool_names()) == TOOLS
    assert [p for p, _ in registry.routers] == ["/plugins/campaign", "/api/plugins/campaign"]
    assert list(registry.steps) == ["install-chromium", "check-setup"]


def test_register_survives_the_host_only_pieces_being_absent(registry):
    campaign.register(registry)
    assert registry.subagents == [], "graph.subagents can't import with no host — and must fail alone"
    assert registry.tools and registry.routers


def test_register_threads_config_through(tmp_path, registry, monkeypatch):
    from campaign import brand, deps, limits

    monkeypatch.delenv("CAMPAIGN_DIR")
    registry.config = {
        "data_dir": str(tmp_path / "custom"),
        "limit_overrides": "github_attachment_video_free: {max_bytes: 100000000}",
        "ffmpeg_path": "/opt/ff/ffmpeg",
        "brand_name": "Acme",
        "bearer_envs": "STAGING_TOKEN",
    }
    campaign.register(registry)
    from campaign import shoot

    assert shoot.bearer_env_problem("STAGING_TOKEN") is None and shoot.bearer_env_problem("OPENAI_API_KEY")
    assert paths.data_dir() == tmp_path / "custom"
    assert limits.get("github_attachment_video_free")["max_bytes"] == 100_000_000
    assert deps._FFMPEG_OVERRIDE == "/opt/ff/ffmpeg"
    assert brand.resolve()["name"] == "Acme"


def test_social_kit_lookup_reads_host_config_lazily(tmp_path, registry):
    # The server fills registry.host AFTER register() — the lookup must not capture a None.
    from campaign import brand

    kit = tmp_path / "s" / "brand-kit.yaml"
    kit.parent.mkdir()
    kit.write_text("brand: Lateco\n")

    class Host:
        config = None

    registry.host = Host()
    campaign.register(registry)

    class Cfg:
        plugin_config = {"social": {"data_dir": str(kit.parent)}}

    registry.host.config = lambda: Cfg()
    assert brand.resolve()["name"] == "Lateco"


def test_every_tool_ships_a_real_description(registry):
    campaign.register(registry)
    for t in registry.tools:
        assert t.description and len(t.description) > 80, f"{t.name}'s description is too thin to route on"


def test_no_tool_can_approve():
    src = (ROOT / "tools.py").read_text(encoding="utf-8")
    assert "store.review(" not in src, "approval is operator-only — only the gated route calls review()"


def test_the_producer_subagent_registers_with_its_allowlist(registry, monkeypatch):
    class SubagentConfig:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.model = None

    graph = types.ModuleType("graph")
    sub = types.ModuleType("graph.subagents")
    cfgmod = types.ModuleType("graph.subagents.config")
    cfgmod.SubagentConfig = SubagentConfig
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.subagents", sub)
    monkeypatch.setitem(sys.modules, "graph.subagents.config", cfgmod)
    registry.config = {"producer_model": "fast-alias"}
    campaign.register(registry)
    (prod,) = registry.subagents
    assert prod.name == "campaign_producer" and prod.model == "fast-alias"
    own = {t for t in prod.tools if t.startswith("campaign_")}
    assert own <= TOOLS and "campaign_shoot" in own and "campaign_render" in own
    assert {"browser_open", "browser_snapshot", "browser_screenshot", "show_artifact"} <= set(prod.tools)
    assert "approve" not in " ".join(prod.tools)


# ── the gallery: four rules ───────────────────────────────────────────────────
@pytest.fixture
def client(registry):
    campaign.register(registry)
    app = FastAPI()
    for prefix, router in registry.routers:
        app.include_router(router, prefix=prefix)
    return TestClient(app)


def test_the_declared_view_path_is_the_path_actually_served(client):
    (v,) = MANIFEST["views"]
    assert v["path"] == "/plugins/campaign/view" and v["icon"] == "Clapperboard"
    assert client.get(v["path"]).status_code == 200
    assert MANIFEST["public_paths"] == [v["path"]]


def test_the_page_is_public_and_the_data_is_not(client):
    assert client.get("/api/plugins/campaign/view").status_code == 404
    assert client.get("/plugins/campaign/campaigns").status_code == 404
    assert client.get("/api/plugins/campaign/campaigns").status_code == 200


def test_page_follows_the_slug_and_design_system_rules():
    page = view.PAGE
    assert 'location.pathname.split("/plugins/")[0]' in page
    assert 'BASE+"/_ds/plugin-kit.css"' in page
    assert 'import(BASE + "/_ds/plugin-kit.js")' in page
    assert 'kit.apiFetch("/api/plugins/campaign" + path' in page
    assert ":root{" not in page and 'addEventListener("message"' not in page
    assert "fetch(BASE + p" in page and page.count("fetch(") == 1, "only the kit-less fallback may bare-fetch"


def test_page_themes_from_tokens():
    for m in re.finditer(r"#[0-9a-fA-F]{3,6}\b", view.PAGE):
        line = view.PAGE[: m.start()].rsplit("\n", 1)[-1] + view.PAGE[m.start() :].split("\n", 1)[0]
        assert "var(--pl-" in line or "&#" in line or "#${" in line, f"hardcoded colour: {line.strip()}"


def test_page_escapes_model_text_and_is_browse_only():
    assert "const esc = (s) =>" in view.PAGE and "esc(a.title" in view.PAGE and "esc(a.notes)" in view.PAGE
    # Browsing only (Josh: don't rebuild chat in a plugin) — no prompt box, no agent calls.
    assert "/chat" not in view.PAGE and "invoke" not in view.PAGE and "Ask the agent…" not in view.PAGE


def _asset_with_file(tmp_path=None, name="clip.webm", outside=False, link=False):
    c = store.create_campaign("Launch")
    cdir = paths.campaign_dir(c["id"], "Launch")
    if outside:
        target = tmp_path / "elsewhere" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"secret")
        p = target
        if link:
            p = cdir / ("link-" + name)
            os.symlink(target, p)
    else:
        p = cdir / name
        p.write_bytes(b"\x1a\x45\xdf\xa3video")
    return store.add_asset(c["id"], "clip", "t", path=str(p), status="captured")


def test_file_route_serves_contained_media_with_its_type(client):
    a = _asset_with_file()
    r = client.get(f"/api/plugins/campaign/file/{a['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "video/webm" and r.content.endswith(b"video")


def test_file_route_refuses_paths_outside_the_media_root(client, tmp_path):
    a = _asset_with_file(tmp_path, outside=True)
    assert client.get(f"/api/plugins/campaign/file/{a['id']}").status_code == 403


def test_file_route_refuses_a_symlink_escape(client, tmp_path):
    a = _asset_with_file(tmp_path, name="x.png", outside=True, link=True)
    assert Path(a["path"]).is_symlink()
    assert client.get(f"/api/plugins/campaign/file/{a['id']}").status_code == 403


def test_file_route_refuses_traversal_strings(client):
    c = store.create_campaign("Launch")
    root = paths.media_root()
    sneaky = str(root / ".." / "campaign.db")
    a = store.add_asset(c["id"], "still", "t", path=sneaky)
    store.list_campaigns()  # make sure the db file exists
    assert client.get(f"/api/plugins/campaign/file/{a['id']}").status_code == 403
    assert client.get("/api/plugins/campaign/file/99999").status_code == 404


@pytest.mark.parametrize("name", ["evil.html", "evil.svg", "evil.htm", "notes.txt"])
def test_file_route_never_serves_a_document_that_could_run_script(client, name):
    c = store.create_campaign("Launch")
    p = paths.campaign_dir(c["id"], "Launch") / name
    p.write_text("<script>parent.fetch('/api/plugins/campaign/assets/1/review')</script>")
    a = store.add_asset(c["id"], "still", "t", path=str(p))
    r = client.get(f"/api/plugins/campaign/file/{a['id']}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]


def test_media_is_served_sandboxed_too(client):
    a = _asset_with_file()
    r = client.get(f"/api/plugins/campaign/file/{a['id']}")
    assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]


def test_no_agent_tool_can_set_approved_or_rejected(registry):
    # Behavioural, not a source grep: drive every status-bearing tool with every operator status.
    campaign.register(registry)
    a = _asset_with_file()
    store.update_asset(a["id"], status="ready_for_review")
    for status in ("approved", "rejected", "APPROVED", " approved "):
        out = registry.tool("campaign_asset_update").invoke({"asset_id": a["id"], "status": status})
        assert "Not updated" in out, out
    assert store.get_asset(a["id"])["status"] == "ready_for_review"


def test_assets_route_flags_unservable_files(client, tmp_path):
    a = _asset_with_file(tmp_path, outside=True)
    data = client.get(f"/api/plugins/campaign/campaigns/{a['campaign_id']}/assets").json()
    assert data["assets"][0]["has_file"] is False
    assert data["statuses"][-2:] == ["approved", "rejected"]


def test_review_route_is_the_operators_approve_and_reject(client, registry):
    a = _asset_with_file()
    store.update_asset(a["id"], status="ready_for_review")
    r = client.post(f"/api/plugins/campaign/assets/{a['id']}/review", json={"decision": "reject", "note": "too fast"})
    assert r.status_code == 200 and r.json()["asset"]["status"] == "rejected"
    assert store.get_asset(a["id"])["review_note"] == "too fast"
    r = client.post(f"/api/plugins/campaign/assets/{a['id']}/review", json={"decision": "approve"})
    assert r.json()["asset"]["status"] == "approved"
    assert (
        "asset_reviewed",
        {"campaign_id": a["campaign_id"], "asset_id": a["id"], "status": "approved"},
    ) in registry.events
    assert client.post(f"/api/plugins/campaign/assets/{a['id']}/review", json={"decision": "meh"}).status_code == 400


def test_campaign_list_route_counts(client):
    a = _asset_with_file()
    store.update_asset(a["id"], status="ready_for_review")
    (c,) = client.get("/api/plugins/campaign/campaigns").json()["campaigns"]
    assert c["assets"] == 1 and c["review"] == 1 and c["approved"] == 0


def test_api_module_has_no_future_annotations():
    # FastAPI resolves string annotations against module globals; the Review model is local.
    lines = (ROOT / "api.py").read_text().splitlines()
    assert not any(ln.startswith("from __future__ import annotations") for ln in lines)
    assert api.MEDIA_TYPES[".mp4"] == "video/mp4"


# ── skills ────────────────────────────────────────────────────────────────────
SKILL_FILES = sorted((ROOT / "skills").glob("*/SKILL.md"))


def test_the_expected_skills_ship():
    assert {p.parent.name for p in SKILL_FILES} == {
        "campaign-planning",
        "shot-scripting",
        "asset-review",
        "montage-editing",
    }


@pytest.mark.parametrize("skill_path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_skill_frontmatter_is_valid_and_names_real_tools(skill_path, registry):
    campaign.register(registry)
    known = set(registry.tool_names())
    text = skill_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front = yaml.safe_load(text.split("---", 2)[1])
    assert front["name"] == skill_path.parent.name
    assert len(front["description"]) > 80 and "Triggers" in front["description"]
    for name in front.get("tools", []):
        if name.startswith("campaign_"):
            assert name in known, f"{skill_path.parent.name} declares unknown tool {name}"


def test_webp_is_served_inline(client):
    c = store.create_campaign("Launch")
    p = paths.campaign_dir(c["id"], "Launch") / "still.webp"
    p.write_bytes(b"RIFF....WEBP")
    a = store.add_asset(c["id"], "still", "t", path=str(p))
    r = client.get(f"/api/plugins/campaign/file/{a['id']}")
    assert r.headers["content-type"] == "image/webp" and "content-disposition" not in r.headers
