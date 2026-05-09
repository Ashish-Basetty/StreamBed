"""Shared routing-table operations used by both /register flows and the health monitor.

All functions here are synchronous DB operations. Network pushes (stream-target updates
to edge daemons) are the caller's responsibility.
"""
import logging

from db import get_connection

logger = logging.getLogger(__name__)


def assign_edge_to_least_loaded_server(cluster: str, edge_id: str) -> str | None:
    """Assign one edge to the cluster server with the fewest existing routes.
    Only servers with a deployment row in the `deployments` table are eligible —
    routing to a server that has no model running is pointless.
    Returns the assigned target server's device_id, or None if no eligible
    server exists.
    """
    conn = get_connection()
    try:
        servers = [
            r[0] for r in conn.execute(
                """SELECT d.device_id
                   FROM devices d
                   INNER JOIN deployments dep
                       ON dep.device_cluster = d.device_cluster
                       AND dep.device_id = d.device_id
                   WHERE d.device_cluster=? AND d.device_type='server'""",
                (cluster,),
            ).fetchall()
        ]
        if not servers:
            return None

        load = {s: 0 for s in servers}
        rows = conn.execute(
            "SELECT target_device, COUNT(*) FROM routing WHERE source_cluster=? GROUP BY target_device",
            (cluster,),
        ).fetchall()
        for target, count in rows:
            if target in load:
                load[target] = count

        target_server = min(load, key=load.get)

        conn.execute(
            """INSERT INTO routing (source_cluster, source_device, target_cluster, target_device, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(source_cluster, source_device) DO UPDATE SET
                   target_cluster=excluded.target_cluster,
                   target_device=excluded.target_device,
                   updated_at=CURRENT_TIMESTAMP""",
            (cluster, edge_id, cluster, target_server),
        )
        conn.commit()
        logger.info(f"[ROUTING] Routed {cluster}/{edge_id} -> {target_server} (load: {load})")
        return target_server
    except Exception as e:
        logger.error(f"[ROUTING] Error assigning route for {cluster}/{edge_id}: {e}")
        return None
    finally:
        conn.close()


def assign_unrouted_edges(cluster: str) -> list[str]:
    """Assign every edge in the cluster that has no routing row.
    Returns the list of edge ids that received a new assignment.
    """
    conn = get_connection()
    try:
        edges = [
            r[0] for r in conn.execute(
                "SELECT device_id FROM devices WHERE device_cluster=? AND device_type='edge'",
                (cluster,),
            ).fetchall()
        ]
        routed = {
            r[0] for r in conn.execute(
                "SELECT source_device FROM routing WHERE source_cluster=?",
                (cluster,),
            ).fetchall()
        }
        unrouted = [e for e in edges if e not in routed]
    finally:
        conn.close()

    assigned: list[str] = []
    for edge_id in unrouted:
        if assign_edge_to_least_loaded_server(cluster, edge_id):
            assigned.append(edge_id)
    return assigned


def orphan_edges_for_server(cluster: str, failed_server: str) -> list[str]:
    """Delete routing rows in `cluster` whose `target_device` is `failed_server`.
    Returns the list of edge ids whose routes were removed.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT source_device FROM routing WHERE source_cluster=? AND target_device=?",
            (cluster, failed_server),
        ).fetchall()
        orphaned = [r[0] for r in rows]
        if orphaned:
            conn.execute(
                "DELETE FROM routing WHERE source_cluster=? AND target_device=?",
                (cluster, failed_server),
            )
            conn.commit()
            logger.info(
                f"[ROUTING] Orphaned {len(orphaned)} edge(s) from failed server "
                f"{cluster}/{failed_server}: {orphaned}"
            )
        return orphaned
    finally:
        conn.close()
