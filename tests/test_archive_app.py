"""GitHub App auth for the protoContent archive (archive.AppAuth), against a mocked GitHub.

What matters: the JWT is a well-formed RS256 app JWT; every installation token is DOWNSCOPED to
the archive repo (the app is installed org-wide); tokens are cached until 5 minutes before they
expire and re-minted once on a 401; the app wins over archive_token; the key, the JWT and the
minted token never show up in an error, a log line or the status; and a key pasted into a
one-line Settings field (newlines gone) still works."""

from __future__ import annotations

import io
import json
import logging
import urllib.error
from datetime import UTC, datetime

import jwt
import pytest
import yaml
from campaign import archive, store
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from test_archive import CFG, NOW, TOKEN, FakeGitHub, _approved, _catalog, c, gh  # noqa: F401 — fixtures

APP_ID, INSTALLATION = 3282126, 121562683
REPO = "protoLabsAI/protoContent"
INST_TOKEN = "ghs_" + "Q" * 36
T0 = 1_800_000_000.0  # the fake clock's "now"


def _keypair():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    return pem, k.public_key()


PEM, PUB = _keypair()
APP_CFG = {"archive_enabled": True, "archive_app_id": APP_ID, "archive_app_private_key": PEM}


@pytest.fixture(autouse=True)
def _fresh_app_cache():
    archive._APPS.clear()
    yield
    archive._APPS.clear()


class _Resp(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


class FakeAPI:
    """A GitHub that answers the app endpoints and records every request."""

    def __init__(self, clock, *, ttl=3600, api=None):
        self.clock, self.ttl = clock, ttl
        self.reqs: list[tuple[str, str, dict, dict | None]] = []
        self.minted = 0
        self.api = api or (lambda req: (200, {"default_branch": "main"}))

    def __call__(self, req, timeout):
        body = json.loads(req.data) if isinstance(req.data, bytes) else None
        path = req.full_url.removeprefix("https://api.github.com")
        self.reqs.append((req.get_method(), path, dict(req.header_items()), body))
        if path == f"/repos/{REPO}/installation":
            return _Resp(200, {"id": INSTALLATION, "app_id": APP_ID})
        if path.endswith("/access_tokens"):
            self.minted += 1
            tok = INST_TOKEN[:-2] + f"{self.minted:02d}"
            return _Resp(201, {"token": tok, "expires_at": _iso(self.clock() + self.ttl)})
        status, got = self.api(req)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(json.dumps(got).encode()))
        return _Resp(status, got)

    def auth_of(self, i):
        return self.reqs[i][2]["Authorization"].removeprefix("Bearer ")


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _auth(api=None, clock=None, installation_id=0, **kw):
    clock = clock or Clock()
    fake = FakeAPI(clock, api=api, **kw)
    return archive.AppAuth(APP_ID, PEM, REPO, installation_id, opener=fake, clock=clock), fake, clock


# ── the JWT ───────────────────────────────────────────────────────────────────
def test_the_jwt_is_an_rs256_app_jwt_backdated_60s_and_good_for_9_minutes():
    a, _, _ = _auth()
    tok = a.jwt()
    assert jwt.get_unverified_header(tok)["alg"] == "RS256"
    claims = jwt.decode(tok, PUB, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iat": int(T0) - 60, "exp": int(T0) + 540, "iss": str(APP_ID)}


def test_the_installation_is_discovered_then_a_repo_scoped_token_is_minted():
    a, fake, _ = _auth()
    assert a.token().startswith("ghs_")
    (m1, p1, h1, b1), (m2, p2, h2, b2) = fake.reqs
    assert (m1, p1, b1) == ("GET", f"/repos/{REPO}/installation", None)
    assert (m2, p2) == ("POST", f"/app/installations/{INSTALLATION}/access_tokens")
    # downscoped: protoContent only, and only these two permissions — whatever the install grants
    assert b2 == {"repositories": ["protoContent"], "permissions": {"contents": "write", "pull_requests": "write"}}
    for i in (0, 1):  # both app calls carry the JWT (verifiable with the app's public key)
        assert jwt.decode(fake.auth_of(i), PUB, algorithms=["RS256"], options={"verify_exp": False,
                                                                               "verify_iat": False})["iss"] == str(APP_ID)  # fmt: skip


def test_a_pinned_installation_id_skips_discovery():
    a, fake, _ = _auth(installation_id=INSTALLATION)
    a.token()
    assert [r[1] for r in fake.reqs] == [f"/app/installations/{INSTALLATION}/access_tokens"]


def test_an_app_not_installed_on_the_repo_says_so():
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b'{"message":"Not Found"}'))

    a = archive.AppAuth(APP_ID, PEM, REPO, opener=opener, clock=Clock())
    with pytest.raises(archive.ArchiveError, match="isn't installed on protoLabsAI/protoContent"):
        a.token()


# ── caching ───────────────────────────────────────────────────────────────────
def test_the_token_is_cached_until_5_minutes_before_it_expires():
    a, fake, clock = _auth()
    first = a.token()
    clock.t += 3600 - 301  # just inside the window
    assert a.token() == first and fake.minted == 1
    clock.t += 2  # 4m59s left: refresh
    second = a.token()
    assert second != first and fake.minted == 2
    # discovery happened once — the installation id is remembered
    assert sum(r[1].endswith("/installation") for r in fake.reqs) == 1


def test_the_auth_is_shared_across_passes():
    s = archive.settings(APP_CFG)
    assert archive.app_auth(s) is archive.app_auth(archive.settings(dict(APP_CFG)))
    other = archive.settings({**APP_CFG, "archive_repo": "protoLabsAI/other"})
    assert archive.app_auth(other) is not archive.app_auth(s)


# ── 401: re-mint once ─────────────────────────────────────────────────────────
def test_a_401_re_mints_the_token_once_and_retries():
    calls = []

    def api(req):
        calls.append(req.get_header("Authorization"))
        return (401, {"message": "Bad credentials"}) if len(calls) == 1 else (200, {"default_branch": "trunk"})

    a, fake, _ = _auth(api=api)
    g = archive.GitHub("", REPO, opener=fake, auth=a)
    assert g.default_branch() == "trunk"
    assert fake.minted == 2 and calls[0] != calls[1] and all(x.startswith("Bearer ghs_") for x in calls)


def test_a_second_401_is_an_error_not_a_loop():
    a, fake, _ = _auth(api=lambda req: (401, {"message": "Bad credentials"}))
    g = archive.GitHub("", REPO, opener=fake, auth=a)
    with pytest.raises(archive.ArchiveError, match="HTTP 401"):
        g.default_branch()
    assert fake.minted == 2


def test_a_retried_upload_resends_the_whole_file(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"0123456789" * 100)
    sent = []

    def opener(req, timeout):
        if req.full_url.startswith("https://uploads.github.com"):
            sent.append(req.data.read())
            if len(sent) == 1:
                raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b"{}"))
            return _Resp(201, {"id": 1, "name": "a.mp4"})
        return fake(req, timeout)

    clock = Clock()
    fake = FakeAPI(clock)
    a = archive.AppAuth(APP_ID, PEM, REPO, INSTALLATION, opener=opener, clock=clock)
    archive.GitHub("", REPO, opener=opener, auth=a).upload_asset(9, "a.mp4", f, "video/mp4")
    assert sent == [f.read_bytes()] * 2


def test_the_token_mode_never_retries_a_401():
    n = []

    def opener(req, timeout):
        n.append(1)
        raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(b"{}"))

    with pytest.raises(archive.ArchiveError):
        archive.GitHub(TOKEN, REPO, opener=opener).default_branch()
    assert len(n) == 1


# ── precedence + the client ───────────────────────────────────────────────────
def test_the_app_wins_over_the_token():
    s = archive.settings({**APP_CFG, "archive_token": TOKEN})
    assert s["auth"] == "app" and archive.problems(s) == []
    fake = FakeAPI(Clock())
    g = archive.client(s, opener=fake)
    g.default_branch()
    used = fake.reqs[-1][2]["Authorization"]
    assert used.startswith("Bearer ghs_") and TOKEN not in json.dumps(fake.reqs)
    assert archive.settings({"archive_token": TOKEN})["auth"] == "token"
    assert archive.settings({})["auth"] == "none"


def test_an_app_id_without_a_key_is_a_problem_never_a_fallback_to_the_token():
    s = archive.settings({"archive_enabled": True, "archive_app_id": APP_ID, "archive_token": TOKEN})
    assert s["auth"] == "app"
    assert any("no archive_app_private_key" in p for p in archive.problems(s))


def test_a_bad_key_or_a_missing_library_is_a_readable_problem(monkeypatch):
    bad = archive.problems(archive.settings({**APP_CFG, "archive_app_private_key": "not a key"}))
    assert any("isn't a PEM private key" in p for p in bad)
    broken = PEM.replace(PEM.splitlines()[5], "A" * 64)
    assert any("doesn't parse" in p for p in archive.problems(archive.settings({**APP_CFG,
                                                                                "archive_app_private_key": broken})))  # fmt: skip
    monkeypatch.setattr(archive, "_jwt_lib", lambda: None)
    assert any("PyJWT" in p for p in archive.problems(archive.settings(APP_CFG)))


def test_the_ids_are_numbers_from_settings():
    s = archive.settings({**APP_CFG, "archive_app_id": "3282126", "archive_app_installation_id": 121562683.0})
    assert (s["app_id"], s["installation_id"]) == (APP_ID, INSTALLATION)
    assert any("isn't a number" in p for p in archive.problems(archive.settings({**APP_CFG,
                                                                                 "archive_app_installation_id": "x"})))  # fmt: skip


# ── the PEM survives Settings and secrets.yaml ────────────────────────────────
@pytest.mark.parametrize(
    "mangle",
    [
        lambda p: p,  # as downloaded
        lambda p: p.replace("\n", ""),  # a one-line <input>: the browser drops the newlines
        lambda p: p.replace("\n", " "),  # …or an editor turned them into spaces
        lambda p: p.replace("\n", "\\n"),  # …or into literal \n escapes
        lambda p: p.replace("\n", "\r\n") + "\n\n",  # Windows line endings, trailing blank lines
    ],
)
def test_a_pasted_key_is_rewrapped_into_the_original_pem(mangle):
    assert archive.normalize_pem(mangle(PEM)) == PEM
    s = archive.settings({**APP_CFG, "archive_app_private_key": mangle(PEM)})
    assert archive.problems(s) == []


def test_a_multiline_key_round_trips_through_secrets_yaml(tmp_path):
    # what core's save_secrets does (yaml.safe_dump) and what an operator writes by hand (a | block)
    dumped = yaml.safe_load(yaml.safe_dump({"campaign": {"archive_app_private_key": PEM.strip()}}))
    assert archive.normalize_pem(dumped["campaign"]["archive_app_private_key"]) == PEM
    block = "campaign:\n  archive_app_private_key: |\n" + "".join(f"    {ln}\n" for ln in PEM.splitlines())
    assert archive.normalize_pem(yaml.safe_load(block)["campaign"]["archive_app_private_key"]) == PEM


def test_the_key_can_come_from_a_file(tmp_path):
    f = tmp_path / "protoquinn.pem"
    f.write_text(PEM)
    cfg = {"archive_enabled": True, "archive_app_id": APP_ID, "archive_app_private_key_path": str(f)}
    s = archive.settings(cfg)
    assert s["app_key"] == PEM and archive.problems(s) == []
    # the secret wins over the file
    assert archive.settings({**cfg, "archive_app_private_key": PEM})["app_key"] == PEM
    missing = archive.settings({**cfg, "archive_app_private_key_path": str(tmp_path / "nope.pem")})
    assert archive.problems(missing) == ["archive_app_private_key_path isn't a file"]


# ── redaction ─────────────────────────────────────────────────────────────────
def test_mint_errors_never_carry_the_key_or_the_jwt():
    seen = {}

    def opener(req, timeout):
        seen["jwt"] = req.get_header("Authorization").removeprefix("Bearer ")
        raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(json.dumps(
            {"message": f"'Expiration time' claim invalid for {seen['jwt']} / {PEM}"}).encode()))  # fmt: skip

    a = archive.AppAuth(APP_ID, PEM, REPO, INSTALLATION, opener=opener, clock=Clock())
    with pytest.raises(archive.ArchiveError) as ei:
        a.token()
    msg = str(ei.value)
    assert "HTTP 401" in msg and seen["jwt"] not in msg and "PRIVATE KEY" not in msg
    assert not any(ln in msg for ln in PEM.splitlines()[1:-1])


def test_an_installation_token_echoed_by_an_api_error_is_redacted(c, caplog):  # noqa: F811
    def api(req):
        tok = req.get_header("Authorization").removeprefix("Bearer ")
        return 500, {"message": f"boom for {tok}"}

    a, fake, _ = _auth(api=api)
    caplog.set_level(logging.DEBUG)
    g = archive.GitHub("", REPO, opener=fake, auth=a)
    _approved(c)
    out = archive.drain(APP_CFG, gh=g, now=NOW)
    blob = json.dumps(out) + caplog.text + json.dumps(store.archive_queue())
    assert out["errors"] and "ghs_" not in blob and "HTTP 500" in blob
    assert archive._redact(f"x {INST_TOKEN} y") == "x *** y"
    assert archive._redact("Bearer " + a.jwt()) == "Bearer ***"
    assert "BEGIN" not in archive._redact(f"key: {PEM}") and PEM.splitlines()[3] not in archive._redact(
        PEM.replace("\n", ""), PEM
    )


def test_a_key_pasted_into_a_drain_error_never_escapes(c, gh, caplog):  # noqa: F811
    def boom():
        raise archive.ArchiveError(f"weird: {PEM.replace(chr(10), '')}")

    gh.default_branch = boom
    _approved(c)
    caplog.set_level(logging.DEBUG)
    out = archive.drain(APP_CFG, gh=gh, now=NOW)
    blob = json.dumps(out) + caplog.text + json.dumps(store.archive_queue())
    assert out["errors"] and not any(ln in blob for ln in PEM.splitlines()[1:-1])


# ── status ────────────────────────────────────────────────────────────────────
def test_status_names_the_auth_mode_and_never_a_secret():
    assert archive.status(APP_CFG)["auth"] == "app"
    assert archive.status({"archive_token": TOKEN})["auth"] == "token"
    assert archive.status({})["auth"] == "none" and archive.status()["auth"] == "none"
    blob = json.dumps(archive.status({**APP_CFG, "archive_token": TOKEN}))
    assert TOKEN not in blob and "PRIVATE" not in blob


# ── commits + the catalog PR under an auto-merging QA bot ─────────────────────
def test_the_catalog_commit_sets_no_author_so_the_app_bot_authors_it():
    bodies = []

    def api(req):
        bodies.append(json.loads(req.data))
        return 201, {"commit": {"sha": "abc"}}

    a, fake, _ = _auth(api=api)
    sha = archive.GitHub("", REPO, opener=fake, auth=a).put_file("specs/x.json", "b", "{}", "msg")
    (body,) = bodies
    assert sha == "abc" and not {"author", "committer"} & set(body)


def test_the_pr_opens_only_after_the_whole_catalog_is_committed(c, gh):  # noqa: F811
    order = []
    put, create = gh.put_file, gh.create_pr
    gh.put_file = lambda *a, **k: (order.append("commit"), put(*a, **k))[1]
    gh.create_pr = lambda *a, **k: (order.append("pr"), create(*a, **k))[1]
    for n in range(3):
        _approved(c, f"a{n}.mp4")
    archive.drain(APP_CFG, gh=gh, now=NOW)
    assert order == ["commit", "pr"]  # one whole-catalog commit, THEN the PR
    assert len(_catalog(gh, "media-catalog/protoagent-github-trending-launch-oct-13")["assets"]) == 3


def test_a_pr_merged_while_its_commit_landed_is_reopened_fresh(c, gh):  # noqa: F811
    _approved(c, "a.mp4")
    archive.drain(APP_CFG, gh=gh, now=NOW)
    (pr1,) = gh.prs
    _approved(c, "b.mp4")

    branch = "media-catalog/protoagent-github-trending-launch-oct-13"
    before = dict(gh.branches[branch])

    def merge_first(_branch):  # protoquinn merged the green PR (holding only `a`) as our push landed
        gh.on_put = None
        gh.branches["main"].update(before)
        pr1["state"] = "merged"

    gh.on_put = merge_first
    archive.drain(APP_CFG, gh=gh, now=NOW)
    assert len(gh.prs) == 2 and gh.prs[1]["state"] == "open"
    assert len(_catalog(gh, branch)["assets"]) == 2  # the fresh PR carries the whole catalog, b included


def test_a_fresh_pr_merged_the_moment_it_opens_is_complete_and_the_next_approval_reopens(c, gh):  # noqa: F811
    # the review's "race on a NEW PR": protoquinn merges it as soon as it exists — nothing is lost,
    # because the whole catalog was committed BEFORE the PR opened and nothing follows in that pass
    branch = "media-catalog/protoagent-github-trending-launch-oct-13"
    create = gh.create_pr

    def create_then_merge(*a, **k):
        pr = create(*a, **k)
        gh.merge(pr)  # merged straight away
        return pr

    gh.create_pr = create_then_merge
    for n in range(2):
        _approved(c, f"a{n}.mp4")
    archive.drain(APP_CFG, gh=gh, now=NOW)
    assert len(_catalog(gh, "main")["assets"]) == 2 and gh.prs[0]["state"] == "merged"
    gh.create_pr = create
    _approved(c, "a2.mp4")
    archive.drain(APP_CFG, gh=gh, now=NOW)
    assert len(gh.prs) == 2 and gh.prs[1]["state"] == "open" and len(_catalog(gh, branch)["assets"]) == 3
