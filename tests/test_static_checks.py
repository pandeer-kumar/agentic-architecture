"""The repository's own validators run as part of the suite."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_contracts_valid():
    r = subprocess.run([sys.executable, str(ROOT / "validation/check_contracts.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_docs_valid():
    r = subprocess.run([sys.executable, str(ROOT / "validation/check_docs.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
