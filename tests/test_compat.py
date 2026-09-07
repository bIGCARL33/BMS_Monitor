"""Guard the Python floor.

The tool runs on whatever interpreter the target board happens to ship, and
that is often not the newest. JetPack 5 (Ubuntu 20.04) is Python 3.8; JetPack 6
(22.04) is 3.10; a current desktop may be 3.13. Code written on the newest of
those breaks silently on the oldest, and the failure lands on a bench with a
battery connected rather than here.

Two layers, because neither is sufficient alone:

* ``vermin`` infers a minimum version from the AST. It is thorough about syntax
  and weak about methods -- it did not catch ``int.bit_count()``, a 3.10 method
  that would have crashed every decode on JetPack 5.
* So an explicit denylist covers the newer built-in *methods* that are easy to
  reach for and impossible to spot by eye.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "jkbms"

#: The oldest interpreter the package must run on: JetPack 5 / Ubuntu 20.04.
MIN_PYTHON = (3, 8)

#: Built-in methods newer than the floor, with the version that added them.
#: Each maps to a portable replacement in the message so a failure is
#: immediately actionable rather than merely a veto.
_TOO_NEW = {
    r"\.bit_count\(": ("3.10", 'use bin(x).count("1")'),
    r"\.removeprefix\(": ("3.9", "use a slice guarded by startswith()"),
    r"\.removesuffix\(": ("3.9", "use a slice guarded by endswith()"),
    r"\bmath\.nextafter\(": ("3.9", "avoid, or vendor the calculation"),
    r"\bfunctools\.cache\b": ("3.9", "use functools.lru_cache(maxsize=None)"),
    r"\bzoneinfo\b": ("3.9", "use datetime.timezone"),
    r"\bgraphlib\b": ("3.9", "avoid"),
    r"\bitertools\.pairwise\(": ("3.10", "zip(seq, seq[1:])"),
    r"\bdatetime\.UTC\b": ("3.11", "use datetime.timezone.utc"),
    r"\btomllib\b": ("3.11", "avoid, or depend on tomli"),
    r"\bStrEnum\b": ("3.11", "use str, Enum"),
    r"\bExceptionGroup\b": ("3.11", "avoid"),
    r"\btyping\.Self\b": ("3.11", 'use a string annotation'),
}


def source_files():
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_there_are_sources_to_check():
    """A silent zero-file scan would make every check below vacuously pass."""
    assert len(source_files()) >= 10


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_methods_newer_than_the_floor(path):
    text = path.read_text()
    # Strip comments so a note *about* a construct does not trip its own guard.
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    for pattern, (version, fix) in _TOO_NEW.items():
        match = re.search(pattern, code)
        assert match is None, (
            f"{path.name} uses {match.group(0)!r}, added in Python {version}, "
            f"above the {MIN_PYTHON[0]}.{MIN_PYTHON[1]} floor. Instead: {fix}")


def test_every_module_uses_future_annotations():
    """Modern annotation syntax on an old interpreter needs the future import."""
    for path in source_files():
        text = path.read_text()
        if not text.strip():
            continue
        assert "from __future__ import annotations" in text, (
            f"{path.name} lacks 'from __future__ import annotations'; "
            f"'X | None' annotations would raise on Python < 3.10 without it")


def _vermin_command() -> str | None:
    """Locate the vermin console script.

    ``python -m vermin`` does not work -- vermin is a package with no
    ``__main__`` -- so the console script is the only entry point, and inside a
    virtualenv it lives beside the interpreter rather than on PATH.
    """
    beside = Path(sys.executable).parent / "vermin"
    if beside.exists():
        return str(beside)
    return shutil.which("vermin")


@pytest.mark.skipif(_vermin_command() is None, reason="vermin not installed")
def test_vermin_agrees_with_the_declared_floor():
    """Catches syntax-level regressions the denylist cannot see."""
    result = subprocess.run(
        # No --eval-annotations: every module carries
        # `from __future__ import annotations`, so `list[int]` and friends stay
        # strings and never need runtime support. The separate
        # test_every_module_uses_future_annotations keeps that true.
        [_vermin_command(), "--no-tips", "-t=3.8", "--violations", str(PACKAGE)],
        capture_output=True, text=True)
    assert result.returncode == 0, (
        f"vermin says the package needs a newer Python than "
        f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}:\n{result.stdout}\n{result.stderr}")
