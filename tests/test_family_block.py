"""The guardian family block is byte-for-byte the same in all four repos: its sha256 is pinned here."""
import hashlib
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"
START, END = "<!-- guardian-family:start -->", "<!-- guardian-family:end -->"
SHA256 = "0cd2ecb7fd53801c8aacae8e4ed48d9712dd97aba8503aca9ffcf0e48d7f8430"


def test_family_block_matches_the_shared_hash():
    text = README.read_bytes().decode("utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if line == START]
    ends = [i for i, line in enumerate(lines) if line == END]
    assert len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0], (starts, ends)
    block = "\n".join(lines[starts[0]:ends[0] + 1]).rstrip("\n")
    assert hashlib.sha256(block.encode("utf-8")).hexdigest() == SHA256
