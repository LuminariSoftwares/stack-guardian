# Release steps (run by the maintainer)

Claude prepared the code and branches; nothing here has been run. Each command is one block, run in a Windows `cmd`
window. Wait for each result before pasting the next.

Branches: `feat/s7-cli` (the `doctor --fix` CLI) and `feat/s10-release-prep` (README, examples, CI, this file). The
second is built on top of the first, so merging it brings both.

**1.** Go to the repo
```
cd /d F:\AI\ClaudeRepos\stack-guardian
```
Expect: the prompt changes to that folder.

**2.** Stop if git left a lock behind
```
if exist .git\index.lock (echo STOP - tell Claude & exit /b 1)
```
Expect: no output.

**3.** Switch to main
```
git checkout main
```
Expect: `Switched to branch 'main'`.

**4.** Merge the release-prep branch (includes the CLI branch)
```
git merge --no-ff feat/s10-release-prep -m "Release prep: doctor --fix CLI, README, examples, CI"
```
Expect: a merge summary listing cli.py, README.md, examples/, .github/workflows/ci.yml.

**5.** Run the tests one last time
```
.venv\Scripts\python.exe -m pytest -q
```
Expect: all tests pass (`N passed`, no failures).

**6.** Create the GitHub repo (in the browser)

Go to https://github.com/new, owner `LuminariSoftwares`, name `stack-guardian`, Public. Leave "Add a README", `.gitignore` and license UNCHECKED (the repo already has them). Click Create repository.
Expect: an empty repo page with setup instructions.

**7.** Point the local repo at it
```
git remote add origin https://github.com/LuminariSoftwares/stack-guardian.git
```
Expect: no output.

**8.** Push main
```
git push -u origin main
```
Expect: `main -> main`. LF/CRLF warnings are harmless.

**9.** Check CI went green (in the browser)

Open https://github.com/LuminariSoftwares/stack-guardian/actions and wait for the `ci` run.
Expect: four green jobs (ubuntu, windows, macos, DSH plugin syntax). If any is red, stop and tell Claude which.

**10.** Tag the release
```
git tag -a v0.1.0.dev0 -m "stack-guardian 0.1.0.dev0 (pre-release)"
```
Expect: no output.

**11.** Push the tag
```
git push origin v0.1.0.dev0
```
Expect: `* [new tag] v0.1.0.dev0 -> v0.1.0.dev0`.

**12.** Publish the GitHub Release (in the browser)

Open https://github.com/LuminariSoftwares/stack-guardian/releases/new, choose tag `v0.1.0.dev0`, title `stack-guardian 0.1.0.dev0`, tick **Set as a pre-release**, click "Generate release notes", then Publish release.
Expect: the release page with the tag.

Optional, with the GitHub CLI instead of step 12:
```
gh release create v0.1.0.dev0 --prerelease --generate-notes --title "stack-guardian 0.1.0.dev0"
```
Expect: the release URL.

## Not part of this release

- PyPI and npm are not published. The version is `0.1.0.dev0` (a PEP 440 dev version, which `pip install` skips unless asked); a stable number must be chosen first, in `pyproject.toml`, and `dsh/package.json` is a separate npm line (`dsh-stack-guardian`). Ask Claude to prep those before any `twine upload` or `npm publish`.
- The `dsh/` plugin ships in the repo as source only.
