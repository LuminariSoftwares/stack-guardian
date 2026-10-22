# Security policy

## Reporting a vulnerability

Report it privately through GitHub: open the repository's
[**Security** tab](https://github.com/LuminariSoftwares/stack-guardian/security/advisories/new) and use
*Report a vulnerability*. That opens a private advisory only the maintainers can see. Please do not open a public
issue for something exploitable, and please do not send it to a personal address; the Security tab is the only
channel that is watched for this.

Tell us what you can: the version (`stack-guardian --version`), what an attacker would need (a nodes file? a rule
folder? a port on your network?), and what they would get. A proof of concept against a local endpoint is ideal. We
aim to acknowledge within a week, and to credit you in the changelog unless you would rather we did not.

Supported: the latest release on PyPI, and `main`. Older versions are not patched.

## What this tool does by design

These are not vulnerabilities; they are the shape of the tool, and worth knowing before you point it at anything.

- **Rules are code.** `--checks-dir DIR` imports every `*.py` in that folder and runs it in-process with your
  privileges. Only point it at rule folders you trust, exactly as you would with a plugin folder.
- **Node URLs are fetched.** Every base URL in your nodes file is requested over HTTP(S) with the standard library's
  `urllib`, which **follows redirects** and sends no credentials. A nodes file that names a host you do not control
  is a request to that host. The file is validated on load (it must be an object of node name to settings, with
  `http://` or `https://` base URLs), but the addresses themselves are taken at your word.
- **`stack-guardian-serve` has no authentication.** It binds to loopback unless you pass `--host H --allow-remote`,
  and it is read-only, but anything that can reach the port can read your snapshot: node names, model names, VRAM
  figures, process names and tailnet state. Bind it to localhost or a tailnet address, not to the open internet.
- **MCP output carries node-controlled text.** `stack_health` and `stack_snapshot` quote what your endpoints
  returned - model names, error strings - and an agent reads that next to its own instructions. The tool
  descriptions say so, and a hostile endpoint on your network could put instruction-shaped text in a model name.
  Treat the reply as data.
- **Typed fixes only, never automatic.** A finding's fix is one of three declared actions (`unload_model`,
  `restart_lane`, `set_env`), never a shell string, and nothing runs without `--apply`. `restart_lane` runs only an
  argv list you declared yourself in `--config`.
- **What is written where.** The state folder (`$STACK_GUARDIAN_STATE` or `~/.stack-guardian/`) holds the run
  history, and after an applied fix also `env`, `fixes.jsonl` and `snapshots/`. The folder is created `0700` and
  those files `0600`, because a snapshot describes your machines and `env` holds values you set. A `set_env` value
  is never written to the audit line, only its length.
- **No runtime dependencies.** The package is standard library only, so a release pulls no third-party code into
  your environment. Test-only extras (`pip install -e ".[test]"`) are not installed by `pip install stack-guardian`.
