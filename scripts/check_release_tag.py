"""Fail a release whose tag is not `v` + the version in pyproject.toml, so PyPI never gets a mislabelled build.

    python scripts/check_release_tag.py v0.3.0     # exit 0 if pyproject.toml says 0.3.0, else exit 1
    python scripts/check_release_tag.py            # no tag (dry run, pull request): print the would-be tag, exit 0
    python scripts/check_release_tag.py --check-pypi [TAG]   # also: exit 1 if that version is already on PyPI

The release workflow runs this before anything is built.
"""
import argparse
import json
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pypi_versions(name, timeout=15):
    """The versions of `name` that PyPI lists, from its public JSON API."""
    with urllib.request.urlopen("https://pypi.org/pypi/%s/json" % name, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        return list(json.load(resp)["releases"])


def _newest(versions):
    def key(v):
        return tuple(int(p) if p.isdigit() else 0 for p in v.replace("-", ".").split("."))
    return max(versions, key=key) if versions else None


def check_pypi(name, version, fetch=None):
    """(exit code, message): 1 when `version` is already on PyPI, which can never be uploaded again.
    A PyPI that cannot be reached is a warning, not a failure: the publish step would say so by itself."""
    try:
        versions = (fetch or pypi_versions)(name)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # the project is not on PyPI yet, so this is its first version
            return 0, "%s %s is new on PyPI (the project is not listed there yet)" % (name, version)
        return 0, "could not ask PyPI about %s (HTTP %s); the publish step will refuse a duplicate anyway" % (name, exc.code)
    except (OSError, ValueError, KeyError) as exc:
        return 0, "could not ask PyPI about %s (%s); the publish step will refuse a duplicate anyway" % (name, type(exc).__name__)
    if version in versions:
        return 1, "%s %s is already on PyPI, and PyPI never accepts a version twice: bump the version" % (name, version)
    return 0, "%s %s is new on PyPI (latest there: %s)" % (name, version, _newest(versions) or "none")


def main(argv=None, fetch=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tag", nargs="?", default="", help="the release tag, e.g. v0.3.0; empty for a dry run")
    ap.add_argument("--pyproject", default=str(ROOT / "pyproject.toml"))
    ap.add_argument("--check-pypi", action="store_true", help="also fail if this version is already on PyPI")
    args = ap.parse_args(argv)
    with open(args.pyproject, "rb") as fh:
        project = tomllib.load(fh)["project"]
    version = project["version"]
    expected = "v" + version
    if not args.tag:
        print("no tag on this run (dry run): pyproject.toml version %s, the release tag would be %s" % (version, expected))
    elif args.tag != expected:
        print("release tag %r does not match pyproject.toml version %s (expected %s)" % (args.tag, version, expected),
              file=sys.stderr)
        return 1
    else:
        print("release tag %s matches pyproject.toml version %s" % (args.tag, version))
    if args.check_pypi:
        code, message = check_pypi(project["name"], version, fetch=fetch)
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
