"""CLI entry-point rename contract: the kestrel command resolves.

Pins the 0.1.0 rename at both invocation surfaces — the Typer app behind
the installed `kestrel` console script, and `python -m kestrel` — plus the
distribution metadata that installs them.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from kestrel.cli import app

runner = CliRunner()
PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


def test_console_app_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "kestrel" in result.output.lower()


def test_python_m_kestrel_help():
    out = subprocess.run(
        [sys.executable, "-m", "kestrel", "--help"],
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip(), "python -m kestrel --help printed nothing"


def test_version_restart():
    """Fresh version lineage: kestrel-agent starts over at 0.1.0."""
    with open(PYPROJECT, "rb") as f:
        data = tomllib.load(f)
    assert data["project"]["name"] == "kestrel-agent"
    assert data["project"]["version"] == "0.1.0"
    assert data["project"]["license"] == "AGPL-3.0-only"


def test_init_version_matches_pyproject():
    import kestrel

    assert kestrel.__version__ == "0.1.0"
