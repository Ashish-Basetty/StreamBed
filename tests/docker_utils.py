"""
Docker compose utilities for integration tests.
"""
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Union, Any


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


class DockerComposeManager:
    """Manages docker-compose services for testing."""

    def __init__(
        self,
        compose_file: Union[str, Path] = "docker-compose.yml",
        project_name: str = "streambed",
        project_root: Optional[Path] = None,
    ):
        self.project_root = Path(project_root) if project_root else _project_root()
        self.compose_file = self.project_root / compose_file if isinstance(compose_file, str) else compose_file
        self.project_name = project_name

    def up_services(
        self,
        services: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        flags: Optional[Dict[str, Any]] = None,
    ) -> subprocess.CompletedProcess:
        """Start docker-compose services.
        env: merged into subprocess environment.
        flags: e.g. {"force_recreate": True} adds --force-recreate."""
        cmd = [
            "docker", "compose",
            "-f", str(self.compose_file),
            "-p", self.project_name,
            "up", "-d",
        ]
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
        cmd = [
            "docker", "compose",
            "-f", str(self.compose_file),
            "-p", self.project_name,
            "start", service,
        ]
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
        cmd = [
            "docker", "compose",
            "-f", str(self.compose_file),
            "-p", self.project_name,
            "down",
        ]
        result = subprocess.run(cmd, cwd=str(self.project_root), capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Warning: Failed to stop services: {result.stderr}")
        return result
