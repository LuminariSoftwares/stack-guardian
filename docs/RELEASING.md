# Releasing stack-guardian

Releases go to PyPI by **trusted publishing**. When a GitHub Release is published,
[`.github/workflows/release.yml`](../.github/workflows/release.yml) checks the tag, runs the tests, builds the sdist
and wheel, and uploads them with a short-lived OIDC credential that PyPI issues to that workflow. No PyPI API token
is stored anywhere. Each uploaded file gets a signed attestation, so anyone can trace it back to the commit it was
built from.

The npm plugin `dsh-stack-guardian` (in `dsh/`) is versioned separately, and this workflow does not publish it.
It is still published by hand.

## One-time setup (the owner does this once)

**Until both steps below are done, publishing a GitHub Release makes the release workflow fail at the "publish to
PyPI" step. Nothing is uploaded.** The tag check, the tests and the build still run, so a failed run is safe. Once
the setup is done, re-run that release with the manual run described under "If a release run failed".

1. **On pypi.org**, signed in as an owner of the `stack-guardian` project:
   1. Go to **Your projects** -> `stack-guardian` -> **Manage** -> **Publishing**.
   2. Under **Add a new publisher**, choose **GitHub**.
   3. Fill in exactly:
      - Owner: `LuminariSoftwares`
      - Repository name: `stack-guardian`
      - Workflow name: `release.yml`
      - Environment name: `pypi`
   4. Click **Add**. The publisher then appears in the list on that page.
2. **On github.com**, in this repository:
   1. Go to **Settings** -> **Environments** -> **New environment**.
   2. Name it `pypi` and click **Configure environment**.
   3. Optional but recommended: tick **Required reviewers** and add yourself. Every publish then waits for your
      click on the run page before anything uploads.
   4. Under **Deployment branches and tags**, choose **Selected branches and tags** and add a tag rule `v*`.
      Only tag builds can then use the environment.
3. Once trusted publishing works, delete the old PyPI API token at pypi.org -> **Account settings** -> **API tokens**,
   and remove any local copy (for example a `~/.pypirc` entry). Nothing uses it any more.

## Each release

1. **Bump-version PR.** On a branch, set `version` in `pyproject.toml` (and any other version fields the previous
   release touched, such as `server.json`) to `X.Y.Z`. Move the `## Unreleased` entries in `CHANGELOG.md` under
   `## stack-guardian X.Y.Z (YYYY-MM-DD)`. Open the PR and wait for CI to pass.
2. **Merge** it into `main`.
3. **Create the GitHub Release**: **Releases** -> **Draft a new release** -> **Choose a tag** -> type `vX.Y.Z`
   (with the `v`) -> **Create new tag on publish**, target `main`. Use `vX.Y.Z` as the title, and paste that
   version's `CHANGELOG.md` section as the notes. Click **Publish release**.
4. **Watch the release workflow**: **Actions** -> **release**. It runs `verify` (fails unless the tag equals
   `v` + the `pyproject.toml` version or that version is already on PyPI, then runs the test suite), `build` (`python -m build` and
   `twine check --strict`), and `pypi` (the upload). If you set required reviewers, approve the `pypi` deployment
   when the run asks.
5. **Verify on PyPI**: open `https://pypi.org/project/stack-guardian/X.Y.Z/` and check that the version is listed
   with both an sdist and a wheel. Under **Download files**, each file should show a provenance attestation
   naming this repository and `release.yml`. Then, in a fresh virtualenv, run
   `pip install stack-guardian==X.Y.Z && stack-guardian --version`.

## If a release run failed

Fix the cause, then go to **Actions** -> **release** -> **Run workflow**. Under **Use workflow from**, pick the
tag `vX.Y.Z` (not `main`): the `v*` rule on the `pypi` environment only lets tag runs publish. Enter the same tag
in the **tag** box and untick **dry_run** to publish it. Leave **dry_run** ticked to verify and build without uploading. A version
that already reached PyPI cannot be uploaded again; it needs a new version number.

If the tag itself is wrong (the `verify` job says it does not match `pyproject.toml`), delete the release and its
tag. Fix the version in a PR, then create the release again.
