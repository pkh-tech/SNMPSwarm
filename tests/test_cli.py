import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "start-swarm"


def run_start(*args):
    return subprocess.run(
        [str(START), *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_help_describes_count_only_pool_model():
    result = run_start("--help")
    assert result.returncode == 0
    assert "--pool-count" in result.stdout
    assert "--pool-percent" not in result.stdout
    assert '"standard unit"' in result.stdout


def test_dash_help_alias_works():
    result = run_start("-help")
    assert result.returncode == 0
    assert "MENTAL MODEL" in result.stdout


def test_start_wrapper_reports_version_without_docker():
    result = run_start("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == "SNMPSwarm 1.00"


def test_config_and_no_config_are_mutually_exclusive():
    result = run_start("--config", "config.example", "--no-config")
    assert result.returncode != 0
    assert "not allowed with argument" in result.stderr


def test_ignore_pools_requires_new_cli_pool():
    result = run_start("--no-config", "--ignore-pools")
    assert result.returncode == 1
    assert "--ignore-pools requires at least one --pool NAME" in (result.stdout + result.stderr)


def test_cli_overallocation_fails_before_docker_is_needed():
    result = run_start(
        "--no-config", "--count", "10",
        "--pool", "a", "--pool-count", "8",
        "--pool", "b", "--pool-count", "3",
    )
    assert result.returncode == 1
    assert "Pool allocation exceeds total_units" in (result.stdout + result.stderr)
