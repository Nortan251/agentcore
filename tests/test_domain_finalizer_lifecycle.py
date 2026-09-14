"""Bounded subprocesses ensure a before-repair deadlock cannot hang pytest."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('family', ['protected', 'action', 'realtime', 'approval'])
@pytest.mark.parametrize('scenario', [
    'acquire_gc', 'release_gc', 'locked_gc', 'shared_gc', 'inversion',
    'concurrent', 'constructor_failure', 'failed_acquire_gc', 'pid_change_in_open',
    'instance_failure', 'instance_failure_shared', 'closing_reentry',
    'fork_in_open', 'fork_in_unlock', 'fork_during_open_thread',
])
def test_domain_lifecycle(family, scenario, tmp_path):
    if scenario.startswith('fork_') and not hasattr(os, 'fork'):
        pytest.skip('POSIX real fork')
    probe = Path(__file__).with_name('domain_finalizer_probe.py')
    env = dict(os.environ, PYTHONPATH=str(probe.parent.parent / 'src'))
    result = subprocess.run(
        [sys.executable, str(probe), family, scenario, str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=7,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"ok": true' in result.stdout
