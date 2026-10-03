"""The API tests drive the app through FastAPI's TestClient, which is Starlette's. Starlette
moved its test client from httpx to httpx2 and warns when only httpx is installed, so the dev
extra installs httpx2. This fails if the client falls back to httpx again."""

import subprocess
import sys


def test_the_test_client_runs_on_httpx2_without_a_deprecation() -> None:
    # A fresh interpreter, since the warning fires once, on first import, and pytest has
    # usually imported the module already while collecting another file.
    probe = (
        "import warnings\n"
        "from starlette.exceptions import StarletteDeprecationWarning\n"
        "warnings.simplefilter('error', StarletteDeprecationWarning)\n"
        "import fastapi.testclient\n"
        "import starlette.testclient\n"
        "print(starlette.testclient.httpx.__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "httpx2"
