"""The release workflow publishes by trusted publishing (OIDC): it must keep its id-token grant and never take a token.

Read as text on purpose (no YAML parser in the test deps): a job is the block of lines indented two spaces under
`jobs:`, and the checks below look only inside the job they are about.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
TAG_CHECK = ROOT / "scripts" / "check_release_tag.py"


def _jobs(text):
    """{job name: its lines} for the top-level `jobs:` mapping."""
    jobs, current, inside = {}, None, False
    for line in text.splitlines():
        if re.match(r"^jobs:\s*$", line):
            inside = True
            continue
        if inside and re.match(r"^\S", line):
            break
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line) if inside else None
        if m:
            current = m.group(1)
            jobs[current] = []
        elif current:
            jobs[current].append(line)
    return jobs


def _problems(text):
    """Every reason this workflow text would not be a token-free trusted-publishing release."""
    problems = []
    jobs = _jobs(text)
    pypi = "\n".join(jobs.get("pypi", []))
    if not pypi:
        problems.append("no pypi job")
    if not re.search(r"^\s+id-token:\s*write\s*$", pypi, re.M):
        problems.append("pypi job lost `id-token: write`")
    if not re.search(r"^\s+environment:\s*(pypi|\n\s+name:\s*pypi)\s*$", pypi, re.M):
        problems.append("pypi job is not in the `pypi` environment")
    if "pypa/gh-action-pypi-publish@" not in pypi:
        problems.append("pypi job does not use pypa/gh-action-pypi-publish")
    if not re.search(r"^\s+attestations:\s*true\s*$", pypi, re.M):
        problems.append("attestations are not on")
    for n, line in enumerate(text.splitlines(), 1):
        code = line.split("#", 1)[0]
        if re.search(r"^\s*password\s*:", code):
            problems.append("line %d: a `password:` input" % n)
        if re.search(r"TOKEN", code, re.I) and "id-token" not in code:
            problems.append("line %d: a token" % n)
        if "secrets." in code:
            problems.append("line %d: reads a secret" % n)
        m = re.search(r"uses:\s*([^\s@]+)@(\S+)", code)
        if m and not re.fullmatch(r"[0-9a-f]{40}", m.group(2)):
            problems.append("line %d: %s is not pinned by commit SHA" % (n, m.group(1)))
    return problems


def test_release_workflow_is_token_free_trusted_publishing():
    assert _problems(WORKFLOW.read_text(encoding="utf-8")) == []


def test_the_guard_catches_each_regression():
    good = WORKFLOW.read_text(encoding="utf-8")
    lost_oidc = good.replace("id-token: write", "id-token: none")
    with_password = good.replace("attestations: true", "attestations: true\n          password: ${{ x }}")
    with_token = good.replace("attestations: true", "attestations: true\n          api-token: abc")
    unpinned = re.sub(r"(pypa/gh-action-pypi-publish@)[0-9a-f]{40}", r"\1release/v1", good)
    assert any("id-token" in p for p in _problems(lost_oidc))
    assert any("password" in p for p in _problems(with_password))
    assert any("token" in p for p in _problems(with_token))
    assert any("not pinned" in p for p in _problems(unpinned))


def _tag_check(*args):
    return subprocess.run([sys.executable, str(TAG_CHECK), *args], capture_output=True, text=True, timeout=60)


def test_tag_check_accepts_the_matching_tag_and_rejects_others(tmp_path):
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "x"\nversion = "1.2.3"\n', encoding="utf-8")
    ok = _tag_check("--pyproject", str(py), "v1.2.3")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    for bad in ("v1.2.4", "1.2.3", "v1.2.3-rc1", "refs/tags/v1.2.3"):
        r = _tag_check("--pyproject", str(py), bad)
        assert r.returncode == 1, (bad, r.stdout, r.stderr)
        assert "1.2.3" in r.stderr


def test_tag_check_without_a_tag_is_a_dry_run_that_names_the_would_be_tag():
    r = _tag_check()
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"no tag .*would be v\d+\.\d+\.\d+", r.stdout)


def _check_pypi(tmp_path, listed, tag="v1.2.3"):
    """check_release_tag.main with a stand-in for PyPI's answer: a list of versions, or an exception to raise."""
    import importlib.util
    import io
    from contextlib import redirect_stderr, redirect_stdout
    spec = importlib.util.spec_from_file_location("check_release_tag", TAG_CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "x"\nversion = "1.2.3"\n', encoding="utf-8")

    def fetch(name):
        assert name == "x"
        if isinstance(listed, Exception):
            raise listed
        return listed
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(["--pyproject", str(py), "--check-pypi", *([tag] if tag else [])], fetch=fetch)
    return code, out.getvalue(), err.getvalue()


def test_check_pypi_says_a_new_version_is_new_and_refuses_one_already_there(tmp_path):
    code, out, _ = _check_pypi(tmp_path, ["1.0.0", "1.2.2", "1.10.0"])
    assert code == 0 and "x 1.2.3 is new on PyPI (latest there: 1.10.0)" in out
    code, out, _ = _check_pypi(tmp_path, ["1.2.2"], tag="")  # the pull request dry run has no tag
    assert code == 0 and "is new on PyPI" in out and "would be v1.2.3" in out
    code, _, err = _check_pypi(tmp_path, ["1.2.2", "1.2.3"])
    assert code == 1 and "already on PyPI" in err


def test_check_pypi_tolerates_a_new_project_and_an_unreachable_pypi(tmp_path):
    import urllib.error
    not_found = urllib.error.HTTPError("https://pypi.org/pypi/x/json", 404, "Not Found", {}, None)
    assert _check_pypi(tmp_path, not_found)[0] == 0 and "not listed there yet" in _check_pypi(tmp_path, not_found)[1]
    code, out, _ = _check_pypi(tmp_path, OSError("no network"))
    assert code == 0 and "could not ask PyPI" in out
    assert _check_pypi(tmp_path, ["1.2.3"], tag="v9.9.9")[0] == 1  # a wrong tag still fails first


def test_the_workflow_asks_pypi_before_building():
    verify = "\n".join(_jobs(WORKFLOW.read_text(encoding="utf-8"))["verify"])
    assert "check_release_tag.py --check-pypi" in verify
