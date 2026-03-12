"""Health monitoring and failover logic for StreamBed controller.

This module:
1. Monitors device heartbeats
2. Detects server failures (missing heartbeats)
3. Automatically reroutes edges to healthy servers
4. Updates stream-target.json via deployment daemons
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from db import (
    get_connection,
    get_device_address,
    get_all_devices_in_cluster,
    get_device_status,
    update_device_status,
)
from shared.interfaces.heartbeat_spec import HeartbeatStatus


logger = logging.getLogger(__name__)


class HealthMonitor:
    """Monitors device health and performs automatic failover."""

    def __init__(
        self,
        heartbeat_timeout_secs: int = 30,
        check_interval_secs: int = 5,
    ):
        """
        Args:
            heartbeat_timeout_secs: Seconds since last heartbeat before marking as unresponsive
            check_interval_secs: Interval between health checks
        """
        self.heartbeat_timeout = timedelta(seconds=heartbeat_timeout_secs)
        self.check_interval = check_interval_secs
        self.running = False

    async def start(self):
        """Start the health monitoring background task."""
        self.running = True
        asyncio.create_task(self._monitor_loop())
        logger.info("Health monitor started")

    async def stop(self):
        """Stop the health monitoring background task."""
        self.running = False
        logger.info("Health monitor stopped")

    async def _monitor_loop(self):
        """Main monitoring loop that checks health and triggers failover."""
        while self.running:
            try:
                await self._check_all_devices()
                await asyncio.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"Error in health check loop: {e}")
                await asyncio.sleep(self.check_interval)

    async def _check_all_devices(self):
        """Check health of all registered devices."""
        conn = get_connection()
        try:
            # Get all device clusters
            clusters = conn.execute(
                "SELECT DISTINCT device_cluster FROM devices"
            ).fetchall()

            for cluster_row in clusters:
                cluster = cluster_row[0]
                await self._check_cluster(cluster)
        finally:
            conn.close()

    async def _check_cluster(self, cluster: str):
        """Check health of all servers in a cluster and failover edges if needed."""
        conn = get_connection()
        try:
            # Get all devices in cluster
            devices = conn.execute(
                "SELECT device_id FROM devices WHERE device_cluster = ?",
                (cluster,),
            ).fetchall()

            for device_row in devices:
                device_id = device_row[0]
                await self._check_device_health(cluster, device_id)

            # After checking all devices, decide if we need to failover edges
            await self._handle_cluster_failover(cluster)
        finally:
            conn.close()

    async def _check_device_health(self, cluster: str, device_id: str):
        """Check if a device has sent a recent heartbeat."""
        status = get_device_status(cluster, device_id)

        if status is None:
            # No heartbeat yet, mark as unresponsive
            logger.warning(f"{cluster}/{device_id}: No heartbeat received yet")
            update_device_status(cluster, device_id, status=HeartbeatStatus.UNRESPONSIVE)
            return

        last_heartbeat = status.get("last_heartbeat")
        if last_heartbeat is None:
            update_device_status(cluster, device_id, status=HeartbeatStatus.UNRESPONSIVE)
            return

        # Parse timestamp from SQLite
        try:
            last_beat_time = datetime.fromisoformat(last_heartbeat)
        except (ValueError, TypeError):
            logger.error(f"{cluster}/{device_id}: Invalid timestamp format")
            update_device_status(cluster, device_id, status=HeartbeatStatus.UNRESPONSIVE)
            return

        time_since_heartbeat = datetime.utcnow() - last_beat_time

        if time_since_heartbeat > self.heartbeat_timeout:
            # No heartbeat within timeout period
            current_status = status.get("status")
            if current_status != HeartbeatStatus.UNRESPONSIVE:
                logger.warning(
                    f"{cluster}/{device_id}: Marked as UNRESPONSIVE "
                    f"(last heartbeat {time_since_heartbeat.total_seconds():.1f}s ago)"
                )
                update_device_status(cluster, device_id, status=HeartbeatStatus.UNRESPONSIVE)
        else:
            # Device is responding
            current_status = status.get("status")
            if current_status != HeartbeatStatus.ACTIVE:
                logger.info(f"{cluster}/{device_id}: Recovered, marked as ACTIVE")
                update_device_status(cluster, device_id, status=HeartbeatStatus.ACTIVE)

    async def _handle_cluster_failover(self, cluster: str):
        """Check if servers have failed and reroute edges to healthy servers."""
        conn = get_connection()
        try:
            # Get all devices and their status
            devices = conn.execute(
                """SELECT device_id, status FROM device_status 
                   WHERE device_cluster = ? ORDER BY device_id""",
                (cluster,),
            ).fetchall()

            device_status_map = {row[0]: row[1] for row in devices}

            # Separate servers from edges
            # (In your architecture, edges and servers may be in same cluster;
            # you can distinguish by device_id prefix or add a device_type column)
            servers = [d for d in device_status_map.keys() if d.startswith("server")]
            edges = [d for d in device_status_map.keys() if d.startswith("edge")]

            # Find healthy servers
            healthy_servers = [
                s for s in servers
                if device_status_map.get(s) == HeartbeatStatus.ACTIVE
            ]

            if not healthy_servers:
                logger.warning(f"{cluster}: No healthy servers available!")
                return

            # Check if any server is down (was previously up)
            down_servers = [
                s for s in servers
                if s not in healthy_servers and device_status_map.get(s) is not None
            ]

            if down_servers:
                logger.warning(
                    f"{cluster}: Servers down: {down_servers}. "
                    f"Rerouting edges to {healthy_servers[0]}"
                )
                # Trigger reroute for all edges
                target_server = healthy_servers[0]
                await self._reroute_edges(cluster, edges, target_server)
        finally:
            conn.close()

    async def _reroute_edges(self, cluster: str, edges: list[str], target_server: str):
        """Reroute all edges to point to the target server."""
        target_addr = get_device_address(cluster, target_server)
        if not target_addr:
            logger.error(f"Cannot find address for {cluster}/{target_server}")
            return

        target_ip, target_port = target_addr

        for edge_id in edges:
            await self._update_edge_target(cluster, edge_id, target_ip, target_port)

    async def _update_edge_target(
        self,
        cluster: str,
        edge_id: str,
        target_ip: str,
        target_port: int,
    ):
        """Update stream-target.json on an edge device via its deployment daemon."""
        edge_addr = get_device_address(cluster, edge_id)
        if not edge_addr:
            logger.error(f"Cannot find address for {cluster}/{edge_id}")
            return

        edge_ip, edge_daemon_port = edge_addr
        daemon_url = f"http://{edge_ip}:{edge_daemon_port}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.put(
                    f"{daemon_url}/stream-target",
                    json={"target_ip": target_ip, "target_port": target_port},
                )
                resp.raise_for_status()
                logger.info(
                    f"{cluster}/{edge_id}: Rerouted to {target_ip}:{target_port}"
                )
        except httpx.HTTPError as e:
            logger.error(
                f"{cluster}/{edge_id}: Failed to update stream-target: {e}"
            )
        except Exception as e:
            logger.error(
                f"{cluster}/{edge_id}: Unexpected error updating stream-target: {e}"
            )


async def create_and_start_monitor(
    heartbeat_timeout_secs: int = 30,
    check_interval_secs: int = 5,
) -> HealthMonitor:
    """Factory function to create and start the health monitor."""
    monitor = HealthMonitor(
        heartbeat_timeout_secs=heartbeat_timeout_secs,
        check_interval_secs=check_interval_secs,
    )
    await monitor.start()
    return monitor
