"""Fetch a marketplace or plugin tree from GitHub, a git URL, a URL to marketplace.json, or a local path.

GitHub repositories download as a tarball through the GitHub API (no git needed). A token in
GITHUB_TOKEN or GH_TOKEN is sent when present, for private repositories and rate limits.
"""
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

GITHUB_SHORT = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
USER_AGENT = "hermes-claude-plugins"
TIMEOUT = 60


class FetchError(Exception):
    pass


def parse_source(text: str) -> dict:
    """Turn a marketplace source string into a source dict.

    owner/repo[#ref]             GitHub
    https://host/x.git[#ref]     git
    git@host:x.git[#ref]         git
    https://host/marketplace.json  a single marketplace file
    /path or ./path or ~/path    local directory
    """
    text = text.strip()
    ref = None
    if "#" in text and not text.startswith(("/", ".", "~")):
        text, ref = text.split("#", 1)
    if text.startswith(("/", "./", "../", "~")) or os.path.isdir(os.path.expanduser(text)):
        return {"source": "local", "path": str(Path(os.path.expanduser(text)).resolve())}
    if text.startswith(("http://", "https://")) and text.endswith(".json"):
        return {"source": "json-url", "url": text}
    if text.startswith(("http://", "https://", "git@", "ssh://")):
        gh = re.match(r"^https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", text)
        if gh:
            return {"source": "github", "repo": gh.group(1), "ref": ref}
        return {"source": "git", "url": text, "ref": ref}
    if GITHUB_SHORT.match(text):
        return {"source": "github", "repo": text, "ref": ref}
    raise FetchError("unrecognised source %r (use owner/repo, a git URL, a URL to marketplace.json, or a local path)" % text)


def describe(source: dict) -> str:
    kind = source.get("source")
    if kind == "github":
        return source["repo"] + (("#" + source["ref"]) if source.get("ref") else "")
    if kind in ("git", "url"):
        return source["url"] + (("#" + source["ref"]) if source.get("ref") else "")
    if kind == "json-url":
        return source["url"]
    return source.get("path", "?")


def _request(url: str, accept: str = "*/*", github: bool = False):
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if github and token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except Exception as exc:
        raise FetchError("download failed for %s: %s" % (url, exc))


def _safe_extract(data: bytes, dest: Path) -> Path:
    """Extract a tarball into dest, refusing absolute paths, parent references and links.
    Returns the single top-level directory GitHub tarballs carry."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        members = []
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise FetchError("archive entry escapes the target: %s" % name)
            if member.issym() or member.islnk():
                continue  # plugins never need links; skipping them removes the escape route
            if not (member.isfile() or member.isdir()):
                continue
            target = (root / name).resolve()
            if root not in target.parents and target != root:
                raise FetchError("archive entry escapes the target: %s" % name)
            members.append(member)
        if hasattr(tarfile, "data_filter"):
            tar.extractall(root, members=members, filter="data")
        else:
            tar.extractall(root, members=members)
    tops = [p for p in root.iterdir()]
    if len(tops) == 1 and tops[0].is_dir():
        return tops[0]
    return root


def _replace_dir(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / (".incoming-" + dest.name)
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(src, staging, ignore=shutil.ignore_patterns(".git"), symlinks=False)
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(staging, dest)


def fetch(source: dict, dest: Path) -> str:
    """Materialise source into dest (replacing it). Returns a revision string when one is known."""
    kind = source.get("source")
    with tempfile.TemporaryDirectory(prefix="hermes-claude-plugins-") as tmp:
        tmp = Path(tmp)
        if kind == "github":
            repo = source["repo"]
            ref = source.get("ref") or ""
            url = "https://api.github.com/repos/%s/tarball/%s" % (repo, ref)
            top = _safe_extract(_request(url, "application/vnd.github+json", github=True), tmp / "x")
            _replace_dir(top, dest)
            rev = top.name.rsplit("-", 1)[-1] if "-" in top.name else ""
            return rev
        if kind in ("git", "url"):
            if not shutil.which("git"):
                raise FetchError("git is not installed, so %s cannot be fetched; use owner/repo for GitHub" % source["url"])
            sha = source.get("sha")
            cmd = ["git", "clone", "--quiet"] + ([] if sha else ["--depth", "1"])
            if source.get("ref") and not sha:
                cmd += ["--branch", source["ref"]]
            cmd += [source["url"], str(tmp / "x")]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                raise FetchError("git clone failed: %s" % proc.stderr.strip()[:400])
            if sha:
                proc = subprocess.run(["git", "-C", str(tmp / "x"), "checkout", "--quiet", sha], capture_output=True, text=True)
                if proc.returncode != 0:
                    raise FetchError("git checkout %s failed: %s" % (sha, proc.stderr.strip()[:400]))
            rev = subprocess.run(["git", "-C", str(tmp / "x"), "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
            _replace_dir(tmp / "x", dest)
            return rev
        if kind == "json-url":
            body = _request(source["url"], "application/json")
            try:
                json.loads(body)
            except ValueError:
                raise FetchError("%s is not JSON" % source["url"])
            (tmp / "x" / ".claude-plugin").mkdir(parents=True)
            (tmp / "x" / ".claude-plugin" / "marketplace.json").write_bytes(body)
            _replace_dir(tmp / "x", dest)
            return ""
        if kind == "local":
            src = Path(source["path"])
            if not src.is_dir():
                raise FetchError("%s is not a directory" % src)
            _replace_dir(src, dest)
            return ""
    raise FetchError("unsupported source type %r" % kind)
