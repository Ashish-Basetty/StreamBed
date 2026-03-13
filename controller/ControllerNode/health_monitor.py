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
    get_last_deployment,
)
from deploy import deploy_to_device, DeployError
from shared.interfaces.heartbeat_spec import HeartbeatStatus


logger = logging.getLogger(__name__)


class HealthMonitor:
    """Monitors device health and performs automatic failover."""

    def __init__(
        self,
        heartbeat_timeout_secs: int = 30,
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

        # Track previous device states
        self.prev_device_states: Dict[str, str] = {}

        # Restart protection
        self.edge_restart_backoff: Dict[str, datetime] = {}
        self.restart_cooldown = timedelta(seconds=30)

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
            logger.info(f"[DEBUG] Monitor loop iteration {loop_count}")
            # allow services time to start
            if datetime.utcnow() - self.start_time < self.startup_grace:
                logger.info("Startup grace period active")
                await asyncio.sleep(self.check_interval)
                continue

            logger.info("[DEBUG] Getting clusters for health check")
            clusters = self._get_clusters()
            logger.info(f"[DEBUG] Clusters found: {clusters}")

            for cluster in clusters:
                logger.info(f"[DEBUG] Evaluating cluster: {cluster}")
                states = await self._evaluate_cluster(cluster)
                logger.info(f"[DEBUG] {cluster} states: {states}")
                # await self._log_edge_server_connections(cluster)
                await self._process_state_transitions(cluster, states)
                self.prev_device_states.update(states)
            await asyncio.sleep(self.check_interval)
            
    # TODO: need to init routing table
    # async def _log_edge_server_connections(self, cluster: str):
    #     """Log which server each edge is currently routed to (for debugging)."""
    #     conn = get_connection()
    #     try:
    #         rows = conn.execute(
    #             """
    #             SELECT device_id, target_server
    #             FROM routing
    #             WHERE device_cluster=?
    #             """,
    #             (cluster,)
    #         ).fetchall()
    #         if not rows:
    #             logger.info(f"[DEBUG] No edge-server routing info for cluster {cluster}")
    #             return
    #         for device_id, target_server in rows:
    #             logger.info(f"[DEBUG] {cluster}: {device_id} routed to {target_server}")
    #     except Exception as e:
    #         logger.error(f"[DEBUG] Error logging edge-server connections: {e}")
    #     finally:
    #         conn.close()

    def _get_clusters(self):
        """Return list of clusters based on device_status table (more robust)."""
        conn = get_connection()

        rows = conn.execute(
            "SELECT DISTINCT device_cluster FROM device_status"
        ).fetchall()

        conn.close()

        return [r[0] for r in rows]

    async def _evaluate_cluster(self, cluster: str):
        """Evaluate current health state of all devices."""

        conn = get_connection()

        rows = conn.execute(
            """
            SELECT device_id, last_heartbeat
            FROM device_status
            WHERE device_cluster=?
            """,
            (cluster,),
        ).fetchall()

        conn.close()

        now = datetime.utcnow()

        states = {}

        for device_id, last_heartbeat in rows:

            if not last_heartbeat:
                states[device_id] = "UNKNOWN"
                continue

            try:
                last = datetime.fromisoformat(last_heartbeat)
            except Exception:
                states[device_id] = "UNRESPONSIVE"
                continue

            if now - last > self.heartbeat_timeout:
                states[device_id] = "UNRESPONSIVE"
            else:
                states[device_id] = "ACTIVE"

        return states

    async def _process_state_transitions(self, cluster: str, states: Dict[str, str]):
        """Trigger actions when device state changes."""

        servers = [d for d in states if d.startswith("server")]
        edges = [d for d in states if d.startswith("edge")]

        healthy_servers = [s for s in servers if states[s] == "ACTIVE"]

        for device, state in states.items():

            prev = self.prev_device_states.get(device)

            if prev == state:
                continue

            logger.info(f"{cluster}/{device}: {prev} -> {state}")

            # SERVER FAILURE EVENT
            # TODO: load balance and attempt server restart
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

    async def _reroute_edges(self, cluster: str, edges, target_server: str):
        """Reroute edges to a healthy server."""

        addr = get_device_address(cluster, target_server)

        if not addr:
            logger.error(f"{cluster}/{target_server}: address unknown")
            return

        target_ip, target_port = addr

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