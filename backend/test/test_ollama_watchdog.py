"""Exercise the actual supervisor with fake Ollama and accelerated polling."""
import os
from pathlib import Path
import subprocess
import time

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "docker/ollama-news-watchdog.sh"


@pytest.fixture
def supervisor(tmp_path):
    if not SCRIPT.exists():
        pytest.skip("Requires repository docker directory")
    commands = {
        "sleep": "#!/bin/sh\nexec /bin/sleep 0.02\n",
        "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
        "ollama": '''#!/usr/bin/env python3
import os, pathlib, sys, time
if sys.argv[1] == 'serve':
    time.sleep(60)
else:
    path = pathlib.Path(os.environ['PROBES'])
    n = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(n))
    mode = os.environ['MODE']
    sys.exit(0 if (mode == 'slow' and n > 5) or (mode == 'runtime' and n == 1) else 1)
''',
    }
    for name, body in commands.items():
        path = tmp_path / name
        path.write_text(body)
        path.chmod(0o755)
    processes = []

    def start(mode, grace):
        env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
               "PROBES": str(tmp_path / "probes"), "MODE": mode,
               "OLLAMA_REQUIRE_GPU": "false", "OLLAMA_STARTUP_GRACE_SECONDS": str(grace)}
        process = subprocess.Popen(["/bin/sh", str(SCRIPT)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(process)
        return process

    yield start
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


def test_slow_start_survives_more_than_three_failed_probes(supervisor):
    process = supervisor("slow", 10)
    time.sleep(1)
    assert process.poll() is None
    process.terminate()
    _, log = process.communicate(timeout=5)
    assert "startup grace" in log
    assert "API ready" in log
    assert "unresponsive" not in log


def test_startup_grace_is_bounded(supervisor):
    process = supervisor("never", 1)
    _, log = process.communicate(timeout=5)
    assert "startup grace expired" in log
    assert process.returncode != 0


def test_runtime_failure_limit_applies_after_ready(supervisor):
    process = supervisor("runtime", 600)
    _, log = process.communicate(timeout=5)
    assert "API ready" in log
    assert "health probe failed (3/3)" in log
    assert process.returncode != 0
