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
    get_device_ip,
    get_device_status,
    get_last_deployment,
    set_device_status_evaluated,
    get_cluster_deployments,
    get_cluster_status,
)
from deploy import delete_container_from_device, deploy_to_device, DeployError
from routing import assign_unrouted_edges, orphan_edges_for_server
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

        # Stream target sync interval
        self.stream_target_sync_interval = timedelta(seconds=30)
        self._last_stream_target_sync: datetime | None = None

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
        while self.running:
            # allow services time to start
            if datetime.utcnow() - self.start_time < self.startup_grace:
                logger.info("Startup grace period active")
                await asyncio.sleep(self.check_interval)
                continue

            await self._routing_tick()
            await asyncio.sleep(self.check_interval)

    async def _routing_tick(self) -> None:
        """One periodic pass over routing concerns.

        Runs every `check_interval`. Two operations, in order:
          1. Failover: for every cluster, evaluate device states and trigger
             orphan + reassign on any server that is UNRESPONSIVE or has no
             deployment row.
          2. Bulk stream-target sync (safety net): every
             `stream_target_sync_interval`, re-push every routing row to its
             edge daemon. This catches any failover push that was lost and
             corrects state that may have drifted out-of-band.
        """
        clusters = self._get_clusters()
        for cluster in clusters:
            states = await self._evaluate_cluster(cluster)
            await self._process_cluster_health(cluster, states)
            self.prev_device_states.update(states)

        now = datetime.utcnow()
        if (
            self._last_stream_target_sync is None
            or now - self._last_stream_target_sync >= self.stream_target_sync_interval
        ):
            await self._sync_stream_targets_from_routing()
            self._last_stream_target_sync = now

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
                last["device_type"],
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
        """Process the health of a cluster.

        A server is considered "failed for routing purposes" if it is either:
          - UNRESPONSIVE (heartbeat timeout), or
          - has no row in the `deployments` table (i.e. "No Model Deployed",
            the same condition the frontend renders as the purple badge).
        """
        servers = [d for d in states if d.startswith("server")]
        deployed = get_cluster_deployments(cluster)  # {device_id: dict}
        healthy_servers = [
            s for s in servers if states[s] == "ACTIVE" and s in deployed
        ]

        for device in servers:
            if states[device] == "UNRESPONSIVE":
                reason = "unresponsive"
            elif device not in deployed:
                reason = "no model deployed"
            else:
                continue

            if not healthy_servers:
                logger.warning(
                    f"{cluster}: server {device} {reason} but no healthy "
                    f"servers available"
                )
                continue

            logger.warning(
                f"{cluster}: server {device} {reason}, orphaning its edges "
                f"and reassigning"
            )
            orphaned = orphan_edges_for_server(cluster, device)
            if not orphaned:
                continue

            # Greedy reassignment across remaining servers (same path /register uses).
            assign_unrouted_edges(cluster)

            # Push the new stream-target to each previously-orphaned edge.
            await self._push_targets_for_edges(cluster, orphaned)

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
            pushed = 0
            for row in rows:
                cluster, edge_id, target_server = row["source_cluster"], row["source_device"], row["target_device"]
                target_ip = get_device_ip(cluster, target_server)
                if not target_ip:
                    logger.warning(f"{cluster}/{target_server}: no IP registered, skipping target push for {edge_id}")
                    continue
                await self._update_edge_target(cluster, edge_id, target_ip, 9000)
                pushed += 1
            if pushed:
                logger.info(f"Synced stream-target for {pushed} edge(s) from routing table")
        except Exception as e:
            logger.error(f"Failed to sync stream-target from routing: {e}")
        finally:
            conn.close()

    async def _push_targets_for_edges(self, cluster: str, edge_ids: list[str]) -> None:
        """Push the current routing target to each edge's deployment daemon.
        Reads the freshly-written routing rows and resolves target IPs.
        """
        if not edge_ids:
            return
        conn = get_connection()
        try:
            placeholders = ",".join("?" for _ in edge_ids)
            rows = conn.execute(
                f"""SELECT source_device, target_device FROM routing
                    WHERE source_cluster=? AND source_device IN ({placeholders})""",
                (cluster, *edge_ids),
            ).fetchall()
        finally:
            conn.close()

        for row in rows:
            edge_id, target_server = row["source_device"], row["target_device"]
            target_ip = get_device_ip(cluster, target_server)
            if not target_ip:
                logger.error(
                    f"{cluster}/{edge_id}: cannot push target, no IP for {target_server}"
                )
                continue
            await self._update_edge_target(cluster, edge_id, target_ip, 9000)

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
    heartbeat_timeout_secs: int = 30,
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