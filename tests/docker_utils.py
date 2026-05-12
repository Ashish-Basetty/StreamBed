"""
Docker compose utilities for integration tests.
"""
import os
import subprocess
from pathlib import Path
from typing import Any, Optional, Union

from shared.docker_labels import MANAGED


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def reap_streambed_runtime_containers(cluster: str = "default") -> None:
    """Remove daemon-created runtime containers left outside docker compose.

    Controller/API cleanup is the source-of-truth path. This is only a final
    recovery sweep for orphaned Docker resources after interrupted tests.
    Labels are METADATA ONLY; legacy name prefixes are included for old builds.
    """
    commands = [
        ["docker", "ps", "-aq", "--filter", f"label={MANAGED}=true"],
        ["docker", "ps", "-aq", "--filter", f"name=streambed-{cluster}-"],
    ]
    ids: set[str] = set()
    for cmd in commands:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            ids.update(result.stdout.strip().splitlines())
    if ids:
        subprocess.run(["docker", "rm", "-f", *sorted(ids)], capture_output=True, text=True)


class DockerComposeManager:
    """Manages docker-compose services for testing."""

    def __init__(
        self,
        compose_file: str | Path = "docker-compose.yml",
        compose_files: list[str | Path] | None = None,
        project_name: str = "streambed",
        project_root: Path | None = None,
    ):
        self.project_root = Path(project_root) if project_root else _project_root()
        self.compose_file = self.project_root / compose_file if isinstance(compose_file, str) else compose_file
        self.compose_files = compose_files
        self.project_name = project_name

    def up_services(
        self,
        services: list[str] | None = None,
        env: dict[str, str] | None = None,
        flags: dict[str, Any] | None = None,
    ) -> subprocess.CompletedProcess:
        """Start docker-compose services.
        env: merged into subprocess environment.
        flags: e.g. {"force_recreate": True, "build": True} adds compose flags."""
        files = self.compose_files
        if files is None:
            files = [self.compose_file]
        else:
            files = [self.project_root / f if isinstance(f, str) else f for f in files]
        cmd = ["docker", "compose"]
        for f in files:
            cmd.extend(["-f", str(f)])
        cmd.extend(["-p", self.project_name, "up", "-d"])
        if flags and flags.get("build"):
            cmd.append("--build")
        if flags and flags.get("force_recreate"):
            cmd.append("--force-recreate")
        if services:
            cmd.extend(services)

        run_env = None
        if env:
            run_env = os.environ.copy()
            run_env.update(env)

        result = subprocess.run(
            cmd,
            cwd=str(self.project_root),
            capture_output=True,
            text=True,
            env=run_env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start services: {result.stderr}")
        return result

    def kill_service(self, service: str) -> subprocess.CompletedProcess:
        """Kill a service container (simulates crash)."""
        container_name = f"{self.project_name}-{service}"
        result = subprocess.run(["docker", "kill", container_name], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to kill service {service}: {result.stderr}")
        return result

    def start_service(self, service: str) -> subprocess.CompletedProcess:
        """Start a specific service."""
        files = self.compose_files
        if files is None:
            files = [self.compose_file]
        else:
            files = [self.project_root / f if isinstance(f, str) else f for f in files]
        cmd = ["docker", "compose"]
        for f in files:
            cmd.extend(["-f", str(f)])
        cmd.extend(["-p", self.project_name, "start", service])
        result = subprocess.run(cmd, cwd=str(self.project_root), capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start service {service}: {result.stderr}")
        return result

    def service_running(self, service: str) -> bool:
        """Check if a service's container is running."""
        container_name = f"{self.project_name}-{service}"
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        return container_name in result.stdout.strip()

    def down_services(self) -> subprocess.CompletedProcess:
        """Tear down all services."""
        files = self.compose_files
        if files is None:
            files = [self.compose_file]
        else:
            files = [self.project_root / f if isinstance(f, str) else f for f in files]
        cmd = ["docker", "compose"]
        for f in files:
            cmd.extend(["-f", str(f)])
        cmd.extend(["-p", self.project_name, "down"])
        result = subprocess.run(cmd, cwd=str(self.project_root), capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Warning: Failed to stop services: {result.stderr}")
        return result
