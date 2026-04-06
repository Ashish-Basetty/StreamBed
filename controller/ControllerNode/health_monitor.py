"""
Health monitoring and failover logic for StreamBed controller.

Features
--------
- Monitors device heartbeats
- Detects server failures
- Automatically reroutes edges to healthy servers
- Restarts failed edge devices
- Uses state-transition logic to prevent reroute/restart storms
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

import httpx

from db import (
    get_connection,
    get_device_address,
    get_device_status,
    get_last_deployment,
    set_device_status_evaluated,
    get_cluster_deployments,
    get_cluster_status,
)
from deploy import delete_container_from_device, deploy_to_device, DeployError
from shared.interfaces.heartbeat_spec import HeartbeatStatus


logger = logging.getLogger(__name__)


class HealthMonitor:
    """Monitors device health and performs automatic failover."""

    def __init__(
        self,
        heartbeat_timeout_secs: int,
        check_interval_secs: int = 5,
        controller_url: Optional[str] = None,
    ):
        self.heartbeat_timeout = timedelta(seconds=heartbeat_timeout_secs)
        self.check_interval = check_interval_secs
        self.controller_url = controller_url

        self.running = False

        # Startup grace period
        self.start_time = datetime.utcnow()
        self.startup_grace = timedelta(seconds=20)
        self._initial_stream_target_synced = False

        # Track previous device states
        self.prev_device_states: Dict[str, str] = {}

        # Restart protection
        self.edge_restart_backoff: Dict[str, datetime] = {}
        self.restart_cooldown = timedelta(seconds=5)

        # Reusable HTTP client
        self.client = httpx.AsyncClient(timeout=10)

    async def start(self):
        """Start background monitoring."""
        self.running = True
        logger.info("[DEBUG] HealthMonitor.start() launching monitor loop")
        asyncio.create_task(self._monitor_loop())
        logger.info("Health monitor started")

    async def stop(self):
        """Stop background monitoring."""
        self.running = False
        await self.client.aclose()
        logger.info("Health monitor stopped")

    async def _monitor_loop(self):
        """Main monitoring loop."""
        logger.info("[DEBUG] Entered HealthMonitor._monitor_loop()")
        loop_count = 0
        while self.running:
            loop_count += 1
            # allow services time to start
            if datetime.utcnow() - self.start_time < self.startup_grace:
                logger.info("Startup grace period active")
                await asyncio.sleep(self.check_interval)
                continue

            # Sync initial stream-target from routing table (once) so edges have a target on deploy
            if not self._initial_stream_target_synced:
                await self._sync_stream_targets_from_routing()
                self._initial_stream_target_synced = True

            clusters = self._get_clusters()

            for cluster in clusters:
                states = await self._evaluate_cluster(cluster)
                # logger.info(f"HealthMonitor: {cluster} states: {states}")
                await self._process_cluster_health(cluster, states)
                self.prev_device_states.update(states)
            await asyncio.sleep(self.check_interval)

    def _get_clusters(self):
        """Return list of clusters based on device_status table (more robust)."""
        conn = get_connection()

        rows = conn.execute(
            "SELECT DISTINCT device_cluster FROM device_status"
        ).fetchall()

        conn.close()

        return [r[0] for r in rows]

    def _restart_delay_for_retry(self, retry_count: int) -> timedelta:
        """Required delay before next restart attempt, as a function of retry_count (exponential backoff)."""
        base_secs = self.restart_cooldown.total_seconds()
        delay_secs = min(base_secs * (2 ** retry_count), 600)  # cap at 10 min
        return timedelta(seconds=delay_secs)

    def _attempt_restart(self, cluster: str, device_id: str) -> bool:
        """Attempt to restart a device via delete + deploy. Returns True on success. Uses increasing delay by retry_count."""
        key = f"{cluster}/{device_id}"
        now = datetime.utcnow()
        status = get_device_status(cluster, device_id)
        retry_count = (status or {}).get("retry_count", 0)
        required_delay = self._restart_delay_for_retry(retry_count)
        if key in self.edge_restart_backoff and now - self.edge_restart_backoff[key] < required_delay:
            logger.info(f"Waiting for restart cooldown: {required_delay}, {cluster}/{device_id}")
            return False
        logger.info(f"Attempting restart {cluster}/{device_id}")
        self.edge_restart_backoff[key] = now
        try:
            delete_container_from_device(cluster, device_id, soft_delete=True)
        except DeployError as e:
            logger.warning(f"Restart {cluster}/{device_id}: delete failed: {e}, continuing")

        last = get_last_deployment(cluster, device_id)
        if not last:
            logger.warning(f"Restart {cluster}/{device_id}: no deployment record, cannot redeploy")
            return False

        try:
            deploy_to_device(
                cluster,
                device_id,
                last["image"],
                last.get("host_port"),
                last.get("container_port"),
            )
            logger.info(f"Restart {cluster}/{device_id}: succeeded")
            return True
        except DeployError as e:
            logger.warning(f"Restart {cluster}/{device_id}: deploy failed: {e}")
            return False

    async def _evaluate_cluster(self, cluster: str):
        """Evaluate current health state of all devices."""

        now = datetime.utcnow()

        states = {}

        expected = get_cluster_deployments(cluster)
        rows = get_cluster_status(cluster)

        for device_id, last_heartbeat in rows:

            if not last_heartbeat:
                states[device_id] = "UNKNOWN"
                set_device_status_evaluated(cluster, device_id, HeartbeatStatus.UNKNOWN)
            else:
                try:
                    last = datetime.fromisoformat(last_heartbeat)
                    if now - last > self.heartbeat_timeout:
                        raise ValueError("heartbeat timeout")
                    else:
                        states[device_id] = "ACTIVE"
                        set_device_status_evaluated(cluster, device_id, HeartbeatStatus.ACTIVE)
                except Exception:
                    states[device_id] = "UNRESPONSIVE"
                    set_device_status_evaluated(cluster, device_id, HeartbeatStatus.UNRESPONSIVE, increment=True)

            # If the device is in the expected deployments and is unresponsive, attempt restart.
            if device_id in expected and states[device_id] not in {"ACTIVE", "UNKNOWN"}:
                asyncio.create_task(asyncio.to_thread(self._attempt_restart, cluster, device_id))


        return states

    async def _process_cluster_health(self, cluster: str, states: Dict[str, str]):
        """Process the health of a cluster."""

        servers = [d for d in states if d.startswith("server")]
        edges = [d for d in states if d.startswith("edge")]

        healthy_servers = [s for s in servers if states[s] == "ACTIVE"]

        for device, state in states.items():

            if device.startswith("server") and state == "UNRESPONSIVE":

                if healthy_servers:
                    logger.warning(
                        f"{cluster}: server failure detected, rerouting edges"
                    )
                    await self._reroute_edges(cluster, edges, healthy_servers[0])
                else:
                    logger.warning(
                        f"{cluster}: server failure but no healthy servers available"
                    )

    async def _sync_stream_targets_from_routing(self) -> None:
        """Push routing table to edge daemons' stream-target. Runs once after startup grace."""
        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT source_cluster, source_device, target_device
                FROM routing
                """
            ).fetchall()
            for row in rows:
                cluster, edge_id, target_server = row["source_cluster"], row["source_device"], row["target_device"]
                await self._update_edge_target(cluster, edge_id, target_server, 9000)
            if rows:
                logger.info(f"Synced stream-target for {len(rows)} edge(s) from routing table")
        except Exception as e:
            logger.error(f"Failed to sync stream-target from routing: {e}")
        finally:
            conn.close()

    async def _reroute_edges(self, cluster: str, edges, target_server: str):
        """Reroute edges to a healthy server.
        Uses target_server (device_id, e.g. server-001) as hostname and port 9000
        (stream listen port). Server containers get a network alias = device_id at deploy.
        """
        target_ip = target_server
        target_port = 9000  # STREAM_LISTEN_PORT on server

        for edge in edges:
            await self._update_edge_target(cluster, edge, target_ip, target_port)
            # Update routing table to reflect new assignment
            self._update_routing_table(cluster, edge, cluster, target_server)

    def _update_routing_table(self, source_cluster, source_device, target_cluster, target_device):
        """Insert or update the routing table for an edge-server assignment."""
        conn = get_connection()
        try:
            conn.execute(
                """
                INSERT INTO routing (source_cluster, source_device, target_cluster, target_device, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_cluster, source_device) DO UPDATE SET
                    target_cluster=excluded.target_cluster,
                    target_device=excluded.target_device,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (source_cluster, source_device, target_cluster, target_device)
            )
            conn.commit()
        except Exception as e:
            logger.error(f"[DEBUG] Error updating routing table: {e}")
        finally:
            conn.close()

    async def _update_edge_target(
        self,
        cluster: str,
        edge_id: str,
        target_ip: str,
        target_port: int,
    ):
        """Send new stream target to edge deployment daemon."""

        addr = get_device_address(cluster, edge_id)

        if not addr:
            logger.error(f"{cluster}/{edge_id}: address unknown")
            return

        edge_ip, daemon_port = addr

        url = f"http://{edge_ip}:{daemon_port}/stream-target"
        try:

            resp = await self.client.put(
                url,
                json={"target_ip": target_ip, "target_port": target_port},
            )

            resp.raise_for_status()

            logger.info(
                f"{cluster}/{edge_id}: rerouted to {target_ip}:{target_port}"
            )

        except httpx.HTTPError as e:

            logger.error(
                f"{cluster}/{edge_id}: failed to update stream target: {e}"
            )




async def create_and_start_monitor(
    heartbeat_timeout_secs: int = 90,
    check_interval_secs: int = 5,
    controller_url: Optional[str] = None,
) -> HealthMonitor:
    """Factory to create and start monitor."""

    monitor = HealthMonitor(
        heartbeat_timeout_secs=heartbeat_timeout_secs,
        check_interval_secs=check_interval_secs,
        controller_url=controller_url,
    )

    await monitor.start()

    return monitor