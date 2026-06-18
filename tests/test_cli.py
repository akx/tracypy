"""Tests for the ``python -m tracypy`` runner (tracypy.__main__)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import tracypy
from tracypy.__main__ import main


@pytest.fixture(autouse=True)
def _isolate_runtime_state():
    # main() rewrites sys.argv and prepends to sys.path and never restores them,
    # and it toggles the profiler — keep all of that from leaking between tests.
    argv, path = sys.argv[:], sys.path[:]
    yield
    sys.argv[:] = argv
    sys.path[:] = path
    tracypy.disable()


# A target script that records the argv, sys.path[0], and profiler state it saw,
# so the test can assert the runner set them up correctly.
_PROBE = """
import json, sys, pathlib
import tracypy
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "argv": sys.argv,
    "name": __name__,
    "enabled": tracypy.is_enabled(),
}))
"""


def test_no_args_is_usage_error_on_stderr(capsys) -> None:
    rc = main([])
    out, err = capsys.readouterr()
    assert rc == 1
    assert "usage:" in err
    assert out == ""


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_goes_to_stdout_with_zero_exit(flag, capsys) -> None:
    rc = main([flag])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "usage:" in out
    assert err == ""


def test_dash_m_without_module_is_error(capsys) -> None:
    rc = main(["-m"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert "usage:" in err


def test_script_mode_runs_target_with_argv_and_profiler_enabled(tmp_path: Path) -> None:
    script = tmp_path / "probe.py"
    script.write_text(_PROBE)
    out = tmp_path / "result.json"

    rc = main([str(script), str(out), "extra"])

    assert rc == 0
    import json

    data = json.loads(out.read_text())
    # The target sees argv == [target, *rest] and runs as __main__ while profiling.
    assert data["argv"] == [str(script), str(out), "extra"]
    assert data["name"] == "__main__"
    assert data["enabled"] is True
    # The runner disabled the profiler again on the way out.
    assert not tracypy.is_enabled()


def test_module_mode_runs_target(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "probe_mod.py").write_text(_PROBE)
    out = tmp_path / "result.json"
    # `python -m` resolves modules against cwd (main inserts "" into sys.path).
    monkeypatch.chdir(tmp_path)

    rc = main(["-m", "probe_mod", str(out)])

    assert rc == 0
    import json

    data = json.loads(out.read_text())
    # runpy.run_module(alter_sys=True) replaces argv[0] with the module's file path.
    assert data["argv"][0].endswith("probe_mod.py")
    assert data["argv"][1:] == [str(out)]
    assert data["name"] == "__main__"


def test_profiler_disabled_even_if_target_raises(tmp_path: Path) -> None:
    script = tmp_path / "boom.py"
    script.write_text("raise RuntimeError('boom')")

    with pytest.raises(RuntimeError, match="boom"):
        main([str(script)])

    # The finally: in main() must have disabled the profiler despite the error.
    assert not tracypy.is_enabled()
