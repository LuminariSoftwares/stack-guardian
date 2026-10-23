"""Fail a release whose tag is not `v` + the version in pyproject.toml, so PyPI never gets a mislabelled build.

    python scripts/check_release_tag.py v0.3.0     # exit 0 if pyproject.toml says 0.3.0, else exit 1
    python scripts/check_release_tag.py            # no tag (dry run, pull request): print the would-be tag, exit 0

The release workflow runs this before anything is built.
"""
import argparse
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tag", nargs="?", default="", help="the release tag, e.g. v0.3.0; empty for a dry run")
    ap.add_argument("--pyproject", default=str(ROOT / "pyproject.toml"))
    args = ap.parse_args(argv)
    with open(args.pyproject, "rb") as fh:
        version = tomllib.load(fh)["project"]["version"]
    expected = "v" + version
    if not args.tag:
        print("no tag on this run (dry run): pyproject.toml version %s, the release tag would be %s" % (version, expected))
        return 0
    if args.tag != expected:
        print("release tag %r does not match pyproject.toml version %s (expected %s)" % (args.tag, version, expected),
              file=sys.stderr)
        return 1
    print("release tag %s matches pyproject.toml version %s" % (args.tag, version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
