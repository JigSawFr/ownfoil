"""Lantern's reference conformance suite, run against a throwaway Ownfoil over fictional games.

It needs a checkout of the Lantern repository (the suite is its `conformance/run.py`): set LANTERN_REPO to
its folder, or the test is skipped.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time

import pytest

REPO = os.environ.get('LANTERN_REPO')
HERE = os.path.dirname(__file__)

# What Ownfoil offers: everything but homebrew, which it does not manage.
LEVELS = {'Lantern Core', '+titles', '+saves', '+cheats', '+requests', '+admin', '+events', '+peers', '+sync',
          '+encrypted_saves', '+deltas'}

pytestmark = pytest.mark.skipif(not REPO or not os.path.isfile(os.path.join(REPO or '', 'conformance', 'run.py')),
                                reason='LANTERN_REPO is not set to a checkout of the Lantern repository')


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_ownfoil_passes_the_suite_at_every_level_it_offers(tmp_path):
    pytest.importorskip('jsonschema')  # the suite's own schema checks (pip install -r tools/requirements.txt)
    pytest.importorskip('referencing')
    port = free_port()
    server = subprocess.Popen([sys.executable, os.path.join(HERE, 'lantern_conformance', 'server.py'), str(tmp_path),
                               str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ready = tmp_path / 'ready.json'
        for _ in range(120):
            if ready.exists() or server.poll() is not None:
                break
            time.sleep(0.5)
        assert ready.exists(), 'the throwaway Ownfoil did not start'
        time.sleep(1)
        info = json.loads(ready.read_text())
        key = tmp_path / 'key'
        key.write_text(info['key'])
        run = subprocess.run([sys.executable, os.path.join(REPO, 'conformance', 'run.py'),
                              f'https://127.0.0.1:{port}/', '--pin', info['pin'], '--token', info['token'],
                              '--key', str(key), '--admin-writes'], capture_output=True, text=True, timeout=900)
    finally:
        server.terminate()
        server.wait(timeout=20)
    output = run.stdout + run.stderr
    assert re.search(r'\b0 failed\b', output), output[-3000:]
    reached = re.search(r'Levels reached[^:]*:\s*(.+)', output).group(1).split()
    named = {w for w in re.split(r'\s+(?=\+|Lantern)', ' '.join(reached)) if w}
    assert LEVELS <= named, f'reached {sorted(named)}'
