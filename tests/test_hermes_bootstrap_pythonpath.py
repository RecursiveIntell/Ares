from __future__ import annotations

import os

import hermes_bootstrap


def test_runtime_root_is_first_without_losing_unrelated_entries(tmp_path):
    runtime_root = tmp_path / "runtime"
    hostile = tmp_path / "hostile"
    userlib = tmp_path / "userlib"
    env = {
        "PYTHONPATH": os.pathsep.join(
            ["", str(hostile), str(runtime_root), str(userlib), str(runtime_root)]
        )
    }

    hermes_bootstrap.pin_runtime_pythonpath(env, str(runtime_root))

    assert env["PYTHONPATH"].split(os.pathsep) == [
        os.path.abspath(runtime_root),
        str(hostile),
        str(userlib),
    ]
