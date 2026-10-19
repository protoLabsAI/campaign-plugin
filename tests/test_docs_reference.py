"""The reference docs must describe the code that exists.

Docs drift because nothing fails when they do. These tests fail.

They check COVERAGE, not prose: every route, agent tool and config key the code exposes appears
in its reference (docs/api.md, docs/tools.md, docs/configuration.md), and each reference names
nothing that no longer exists. What each entry MEANS is a human's job; this only guarantees the
lists are honest. Everything is read by AST or from the manifest, never by importing the plugin,
so the guard can't be fooled by a name that only appears in a comment.
"""

from __future__ import annotations

import ast
import pathlib
import re

import yaml

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DOCS = _ROOT / "docs"
_API_DOC = _DOCS / "api.md"
_TOOLS_DOC = _DOCS / "tools.md"
_CONFIG_DOC = _DOCS / "configuration.md"
_MANIFEST = yaml.safe_load((_ROOT / "protoagent.plugin.yaml").read_text())

_VERBS = ("get", "post", "put", "patch", "delete")


# ── routes ───────────────────────────────────────────────────────────────────────────


def _mount_prefixes() -> dict[str, str]:
    """``{builder function name: prefix}`` from ``__init__.py``'s
    ``registry.register_router(build_x(...), prefix="...")`` calls — so a router moved to a new
    prefix moves its documented paths with it."""
    out: dict[str, str] = {}
    for node in ast.walk(ast.parse((_ROOT / "__init__.py").read_text())):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "register_router"):
            continue
        builder = node.args[0] if node.args else None
        prefix = next((k.value for k in node.keywords if k.arg == "prefix"), None)
        if (
            isinstance(builder, ast.Call)
            and isinstance(builder.func, ast.Name)
            and isinstance(prefix, ast.Constant)
            and isinstance(prefix.value, str)
        ):
            out[builder.func.id] = prefix.value
    return out


def _routes() -> set[tuple[str, str]]:
    """(VERB, full path) for every route every mounted router registers.

    Each builder (``build_view_router`` / ``build_data_router`` in api.py,
    ``build_archive_router`` in archive_api.py) makes its own ``APIRouter()``; the route
    decorators are ``@<router var>.get(...)`` etc. inside it, whatever the variable is called."""
    prefixes = _mount_prefixes()
    found: set[tuple[str, str]] = set()
    seen_builders: set[str] = set()
    for path in sorted(_ROOT.glob("*.py")):
        for fn in ast.parse(path.read_text()).body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name in prefixes):
                continue
            seen_builders.add(fn.name)
            routers = {
                t.id
                for n in ast.walk(fn)
                if isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", getattr(n.value.func, "attr", "")) == "APIRouter"
                for t in n.targets
                if isinstance(t, ast.Name)
            }
            for node in ast.walk(fn):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                        continue
                    owner = getattr(dec.func.value, "id", "")
                    if dec.func.attr in _VERBS and owner in routers:
                        arg = dec.args[0] if dec.args else None
                        assert isinstance(arg, ast.Constant), f"{path.name}:{node.name}: non-literal route path"
                        found.add((dec.func.attr.upper(), prefixes[fn.name] + arg.value))
    assert seen_builders == set(prefixes), f"mounted builders not found: {set(prefixes) - seen_builders}"
    return found


def test_the_route_extractor_sees_all_three_routers():
    """A guard that silently finds nothing passes forever. Pin that it really reads each router."""
    routes = _routes()
    assert ("GET", "/plugins/campaign/view") in routes
    assert ("POST", "/api/plugins/campaign/assets/{asset_id}/review") in routes
    assert ("GET", "/api/plugins/campaign/archive") in routes
    assert len(routes) >= 16


def test_every_route_is_in_the_api_reference():
    doc = _API_DOC.read_text()
    missing = sorted(f"{v} {p}" for v, p in _routes() if f"| `{v}` | `{p}` |" not in doc)
    assert not missing, (
        f"{len(missing)} route(s) are not in docs/api.md: {missing}. Add a row "
        f"| `VERB` | `/full/path` | what it is FOR |."
    )


def test_the_api_reference_names_no_route_that_was_removed():
    documented = set(re.findall(r"^\| `(GET|POST|PUT|PATCH|DELETE)` \| `([^`]+)` \|", _API_DOC.read_text(), re.M))
    stale = sorted(f"{v} {p}" for v, p in documented - _routes())
    assert not stale, f"docs/api.md documents {len(stale)} route(s) that no longer exist: {stale}"


# ── tools ────────────────────────────────────────────────────────────────────────────


def _tools() -> set[str]:
    """Every ``@tool``-decorated function in tools.py (def or async def)."""
    out: set[str] = set()
    for n in ast.walk(ast.parse((_ROOT / "tools.py").read_text())):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in n.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if getattr(target, "id", getattr(target, "attr", "")) == "tool":
                    out.add(n.name)
    return out


def test_the_tool_extractor_finds_the_tools():
    tools = _tools()
    assert {"campaign_create", "campaign_shoot", "campaign_produce_video", "campaign_prune"} <= tools
    assert all(t.startswith("campaign_") for t in tools), sorted(t for t in tools if not t.startswith("campaign_"))


def test_every_agent_tool_is_in_the_tools_reference():
    table = _tools_table_names()
    missing = sorted(_tools() - table)
    assert not missing, (
        f"{len(missing)} tool(s) are not in docs/tools.md's table: {missing}. Any agent on the "
        f"instance can call them; an undocumented one is a capability nobody can audit."
    )


def _tools_table_names() -> set[str]:
    """Tool names in the FIRST cell of each table row (rows may group several: `a` / `b`)."""
    names: set[str] = set()
    for line in _TOOLS_DOC.read_text().splitlines():
        if line.startswith("| `campaign_"):
            names |= set(re.findall(r"`(campaign_[a-z_]+)`", line.split("|")[1]))
    return names


def test_the_tools_reference_names_no_tool_that_was_removed():
    stale = sorted(_tools_table_names() - _tools())
    assert not stale, f"docs/tools.md documents {len(stale)} tool(s) that no longer exist: {stale}"


# ── config keys ──────────────────────────────────────────────────────────────────────


def _config_keys() -> set[str]:
    return set(_MANIFEST.get("config") or {})


def _config_rows() -> dict[str, list[str]]:
    """``{key: cells}`` for each row of the Keys table — a row whose second cell is a
    backticked default, which is what separates it from the live/reload legend rows."""
    rows: dict[str, list[str]] = {}
    for line in _CONFIG_DOC.read_text().splitlines():
        m = re.match(r"^\| `([a-z_0-9]+)` \| `", line)
        if m:
            rows[m.group(1)] = [c.strip() for c in line.strip().strip("|").split("|")]
    return rows


def test_every_config_key_is_documented():
    missing = sorted(_config_keys() - set(_config_rows()))
    assert not missing, (
        f"{len(missing)} config key(s) in protoagent.plugin.yaml are not rows in "
        f"docs/configuration.md: {missing}. Add them with a default and live / reload."
    )


def test_the_configuration_reference_names_no_key_that_was_removed():
    stale = sorted(set(_config_rows()) - _config_keys())
    assert not stale, f"docs/configuration.md documents {len(stale)} key(s) the manifest no longer has: {stale}"


def test_the_operator_only_column_matches_the_manifest():
    """`yes` = `spawns: true` (the agent's set_config refuses it); `secret` = in `secrets:`."""
    spawns = {f["key"] for f in _MANIFEST.get("settings") or [] if isinstance(f, dict) and f.get("spawns")}
    secrets = set(_MANIFEST.get("secrets") or [])
    wrong = []
    for key, cells in _config_rows().items():
        want = "secret" if key in secrets else "yes" if key in spawns else ""
        if cells[4] != want:
            wrong.append(f"{key}: doc says {cells[4]!r}, manifest says {want!r}")
    assert not wrong, f"docs/configuration.md's Operator-only column is wrong: {wrong}"


# ── install instructions ─────────────────────────────────────────────────────────────

_INSTALL_DOCS = (_ROOT / "README.md", _DOCS / "install.md")


def test_the_install_ref_is_the_current_version():
    """The install line pins a tag; it must be the version this tree releases as, or a reader
    installs an older plugin than the docs describe."""
    want = f"v{_MANIFEST['version']}"
    for doc in _INSTALL_DOCS:
        text = doc.read_text()
        refs = re.findall(r"--ref (v[0-9][0-9A-Za-z.\-]*[0-9A-Za-z])", text) + re.findall(r"ref `(v[^`]+)`", text)
        assert refs, f"{doc.name} lost its pinned install ref"
        bad = sorted({r for r in refs if r != want})
        assert not bad, f"{doc.name} installs {bad}; the manifest is {want}. Bump the ref with the release."


def test_the_install_docs_name_only_real_server_subcommands():
    """A plausible-looking `python -m server plugin <verb>` that doesn't exist fails a stranger
    on the second line of setup. The real list, from protoAgent's `python -m server plugin --help`."""
    real = {
        "new",
        "new-bundle",
        "install",
        "list",
        "uninstall",
        "update-bundle",
        "uninstall-bundle",
        "sync",
        "install-deps",
    }
    claimed = set()
    for doc in _INSTALL_DOCS:
        claimed |= set(re.findall(r"python -m server plugin ([a-z][a-z-]*)", doc.read_text()))
    assert claimed, "the install docs lost their install command"
    assert not claimed - real, f"not a real subcommand: {sorted(claimed - real)}"


# ── layout ───────────────────────────────────────────────────────────────────────────


def test_the_layout_table_names_only_real_paths():
    body = (_DOCS / "development.md").read_text().split("## Layout", 1)
    assert len(body) == 2, "docs/development.md lost its Layout section"
    names = re.findall(r"^\| `([A-Za-z0-9_./-]+)` \|", body[1].split("\n## ", 1)[0], re.M)
    assert names, "the Layout table is empty"
    gone = [n for n in names if not (_ROOT / n).exists()]
    assert not gone, f"docs/development.md's Layout table names paths that don't exist: {gone}"


def test_the_manifest_points_at_docs_that_exist():
    """Settings descriptions send the operator to a docs page; it has to be there."""
    text = (_ROOT / "protoagent.plugin.yaml").read_text()
    assert "README's" not in text, "the manifest still points at a README section that moved to docs/"
    for ref in set(re.findall(r"docs/[a-z0-9-]+\.md", text)):
        assert (_ROOT / ref).is_file(), f"the manifest cites {ref}, which doesn't exist"


def test_every_image_the_docs_show_is_in_the_repo():
    """A relative <img src> or ![](…) that points nowhere renders as a broken image on GitHub."""
    missing = []
    for doc in [_ROOT / "README.md", *sorted(_DOCS.glob("*.md"))]:
        text = doc.read_text()
        for src in re.findall(r'<img[^>]*\bsrc="([^"]+)"', text) + re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text):
            if not re.match(r"https?:", src) and not (doc.parent / src).is_file():
                missing.append(f"{doc.relative_to(_ROOT)} → {src}")
    assert not missing, f"images referenced but not in the repo: {missing}"
