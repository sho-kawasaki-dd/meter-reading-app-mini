from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from meter_reading_mini.ui.single_instance import SingleInstanceLock

LOCK_PATH_ENV = "METER_READING_MINI_TEST_LOCK_PATH"
LOCK_HOLDER = """
import os
import sys
from pathlib import Path
from meter_reading_mini.ui.single_instance import SingleInstanceLock

lock = SingleInstanceLock(Path(os.environ["METER_READING_MINI_TEST_LOCK_PATH"]))
if not lock.try_acquire(1000):
    raise SystemExit(2)
print("acquired", flush=True)
sys.stdin.readline()
lock.release()
"""


def start_lock_holder(path: Path) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env[LOCK_PATH_ENV] = str(path)
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{src_dir}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else src_dir
    )
    process = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "acquired"
    return process


def stop_lock_holder(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
    process.communicate(timeout=10)


def test_lock_can_be_reacquired_after_release(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "meter_reading.lock")

    assert lock.try_acquire()
    lock.release()
    lock.release()
    assert lock.try_acquire()
    lock.release()


def test_lock_is_unavailable_while_another_process_holds_it(tmp_path: Path) -> None:
    path = tmp_path / "meter_reading.lock"
    process = start_lock_holder(path)
    lock = SingleInstanceLock(path)

    try:
        assert not lock.try_acquire()
        assert process.stdin is not None
        process.stdin.write("release\n")
        process.stdin.flush()
        process.wait(timeout=10)

        assert lock.try_acquire()
        lock.release()
    finally:
        stop_lock_holder(process)


def test_stale_lock_is_recovered_after_process_is_killed(tmp_path: Path) -> None:
    path = tmp_path / "meter_reading.lock"
    process = start_lock_holder(path)
    lock = SingleInstanceLock(path)

    try:
        assert not lock.try_acquire()
        process.kill()
        process.wait(timeout=10)

        assert lock.try_acquire()
        lock.release()
    finally:
        stop_lock_holder(process)
