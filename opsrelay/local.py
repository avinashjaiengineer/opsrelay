"""`opsrelay up`: the whole AgentCore topology on your machine, without Docker.

Starts the four specialist agents as separate A2A servers (ports 9001-9004 by default) and the
coordinator on port 8080 with the same /invocations contract as AgentCore. The coordinator
reaches the specialists over real A2A HTTP calls; all of them share a local SQLite file.
"""

import os
import threading
import time
import webbrowser
from importlib.resources import files

import httpx
import uvicorn
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from .config import SPECIALISTS, get_settings
from .store import get_store


def _wait_until_up(urls: list[str], timeout: float = 30) -> None:
    deadline = time.time() + timeout
    for url in urls:
        while True:
            try:
                if httpx.get(url + "/ping", timeout=2).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.time() > deadline:
                raise RuntimeError(f"{url} did not start; is the port already in use?")
            time.sleep(0.2)


def add_dashboard(app) -> None:  # noqa: ANN001
    """Serve the dashboard at GET / (local runs only; the AgentCore runtime serves just the API)."""
    html = files("opsrelay").joinpath("dashboard.html").read_text(encoding="utf-8")

    async def dashboard(_request: Request) -> HTMLResponse:
        return HTMLResponse(html)

    if not any(getattr(r, "path", None) == "/" for r in app.router.routes):
        app.router.routes.insert(0, Route("/", dashboard, methods=["GET"]))


def run_local_stack(
    host: str = "127.0.0.1", port: int = 8080, specialist_base_port: int = 9001, open_browser: bool = True
) -> None:
    """`host` is where the coordinator and dashboard listen; the specialists always stay on loopback."""
    urls = {role: f"http://127.0.0.1:{specialist_base_port + i}" for i, role in enumerate(SPECIALISTS)}
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host  # noqa: S104 - a bind address, not a URL

    # Point the coordinator at the specialists over A2A before anything reads the settings.
    os.environ["OPSRELAY_SPECIALIST_TRANSPORT"] = "a2a"
    for role, url in urls.items():
        os.environ[f"OPSRELAY_{role.upper()}_ENDPOINT"] = url
    get_settings.cache_clear()
    get_store.cache_clear()

    from .runtime.coordinator import app
    from .runtime.specialist import build_app

    for role, url in urls.items():
        config = uvicorn.Config(
            build_app(role, url + "/"), host="127.0.0.1", port=int(url.rsplit(":", 1)[1]), log_level="warning"
        )
        threading.Thread(target=uvicorn.Server(config).run, name=f"a2a-{role}", daemon=True).start()
    _wait_until_up(list(urls.values()))

    settings = get_settings()
    print("OpsRelay is running locally (Ctrl+C to stop)")
    print(
        f"  model:        {settings.model_provider}"
        + (f" ({settings.bedrock_model_id})" if settings.model_provider == "bedrock" else "")
    )
    print(f"  {'state:':<16}{os.path.abspath(settings.sqlite_path) if settings.store == 'sqlite' else settings.store}")
    for role, url in urls.items():
        print(f"  {role + ':':<16}{url}/.well-known/agent-card.json  (A2A)")
    print(f"  {'coordinator:':<16}http://{shown_host}:{port}/invocations")
    if shown_host != host:
        print(f"  {'listening on:':<16}{host}:{port} (reachable from other machines)")
    dashboard_url = f"http://{shown_host}:{port}/"
    print(f"\n  Dashboard:  {dashboard_url}\n")
    print(f"Or from another terminal:  opsrelay --url http://{shown_host}:{port} simulate bad-deploy\n")
    add_dashboard(app)
    if open_browser:
        threading.Timer(1.5, webbrowser.open, args=[dashboard_url]).start()
    app.run(port=port, host=host)
