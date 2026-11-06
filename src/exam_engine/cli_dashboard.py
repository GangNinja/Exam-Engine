"""``exam-engine dashboard`` - the Phase 12 local workspace.

The dashboard is a thin HTTP adapter over the existing service layer. It binds
to loopback by default because it has no authentication; exposing it on a LAN
is an explicit operator decision.
"""

from __future__ import annotations

from collections.abc import Callable

import typer

from exam_engine.config import Settings
from exam_engine.dashboard.api import DashboardAPI
from exam_engine.dashboard.server import create_server

GetSettings = Callable[[], Settings]

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def register_dashboard_commands(app: typer.Typer, *, get_settings: GetSettings) -> None:
    @app.command("dashboard")
    def dashboard(
        host: str = typer.Option(
            "127.0.0.1",
            "--host",
            help="Bind address. Loopback only unless --allow-remote is set.",
        ),
        port: int = typer.Option(
            8765,
            "--port",
            min=0,
            max=65535,
            help="TCP port (0 chooses an ephemeral port).",
        ),
        allow_remote: bool = typer.Option(
            False,
            "--allow-remote",
            help="Allow a non-loopback bind. The dashboard has no accounts or TLS.",
        ),
    ) -> None:
        """Serve the local educational-intelligence workspace."""

        if host not in _LOOPBACK and not allow_remote:
            typer.echo(
                "ERROR: the dashboard has no authentication and binds to loopback only. "
                "Use --allow-remote to override deliberately.",
                err=True,
            )
            raise typer.Exit(code=2)
        settings = get_settings()
        try:
            api = DashboardAPI(settings.db_path, settings)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            typer.echo(f"ERROR: could not open the dashboard database: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        try:
            server = create_server(api, host, port)
        except OSError as exc:
            api.close()
            typer.echo(f"ERROR: could not bind {host}:{port}: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        raw_host, raw_port = server.server_address[0], server.server_address[1]
        bound_host = raw_host.decode("ascii") if isinstance(raw_host, bytes) else raw_host
        bound_port = int(raw_port)
        display_host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else bound_host
        typer.echo(f"Exam Engine dashboard: http://{display_host}:{bound_port}")
        typer.echo(f"database: {settings.db_path}")
        if host not in _LOOPBACK:
            typer.echo(
                "WARNING: no authentication, no TLS, and analysis jobs can write to this "
                "database. Only expose this on a trusted network.",
                err=True,
            )
        typer.echo("Press Ctrl+C to stop.")
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            typer.echo("\nstopped")
        finally:
            server.server_close()
            api.close()


__all__ = ["register_dashboard_commands"]
