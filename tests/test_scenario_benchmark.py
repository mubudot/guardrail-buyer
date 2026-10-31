"""The committed report must reproduce, and a mismatch must fail the command."""
import importlib.util
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_scenario_benchmark_matches_report():
    result = subprocess.run([sys.executable, "scripts/scenario_benchmark.py"], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "17/17 scenarios passed." in result.stdout
    assert result.stdout == (ROOT / "docs/SCENARIO_BENCHMARK.md").read_text()


def test_scenario_benchmark_exits_nonzero_on_mismatch(monkeypatch, capsys):
    for key, value in {"AGENT_MODE": "offline", "PAYPAL_MODE": "mock", "GRB_NO_DOTENV": "1",
                       "GRB_NO_AUTOAPP": "1"}.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("scenario_benchmark", ROOT / "scripts/scenario_benchmark.py")
    benchmark = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, benchmark)
    spec.loader.exec_module(benchmark)
    monkeypatch.setattr(benchmark, "SCENARIOS", (replace(benchmark.SCENARIOS[0], reserved="0.00"),))
    assert benchmark.main() == 1
    assert "| FAIL |" in capsys.readouterr().out
