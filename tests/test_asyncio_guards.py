"""Guard tests for the suite's un-awaited-awaitable detection.

pyproject.toml promotes ``RuntimeWarning`` and
``pytest.PytestUnraisableExceptionWarning`` to test failures, and
``tests/conftest.py`` fails a test when asyncio logs "Task was destroyed but
it is pending!" / "... was never awaited". The tests here prove that this
harness actually catches awaitables that are never awaited.
"""

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_suite_fails_on_unawaited_coroutine(tmp_path: pathlib.Path) -> None:
    """A test that drops a coroutine must fail under the suite's config.

    Runs a scratch test file through pytest with this repo's pyproject.toml
    as the config, so the assertion exercises the real ``filterwarnings``
    setup rather than a copy of it.

    A separate process fork is necessary as __del__ on a coroutine raises when
    it is not awaited and propagates to sys.unraisablehook, not to the caller
    (so we cannot assert properly here).
    """
    test_file = tmp_path / "test_unawaited_coroutine.py"
    test_file.write_text(
        "def test_unawaited():\n    async def coro():\n        pass\n    coro()\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
            "-p",
            "no:cacheprovider",
            "-q",
            str(test_file),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode != 0
    assert "coroutine" in proc.stdout
