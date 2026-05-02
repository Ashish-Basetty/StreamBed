function app() {
  return {
    API: 'http://localhost:8080',
    clusters: [],
    error: null,
    lastRefresh: '',
    toast: { visible: false, msg: '', ok: true },

    async init() {
      await this.refresh();
      setInterval(() => this.refresh(), 10000);
    },

    async refresh() {
      this.error = null;
      try {
        // Discover clusters, status, and routing in parallel.
        const [clustersRes, statusRes, routingRes] = await Promise.all([
          fetch(`${this.API}/clusters`),
          fetch(`${this.API}/status`),
          fetch(`${this.API}/routing`),
        ]);
        if (!clustersRes.ok) throw new Error('clusters: ' + (await clustersRes.text()));
        if (!statusRes.ok) throw new Error('status: ' + (await statusRes.text()));
        if (!routingRes.ok) throw new Error('routing: ' + (await routingRes.text()));
        const clusterNames = (await clustersRes.json()).clusters || [];
        const statusList = (await statusRes.json()).status || [];
        const routing = (await routingRes.json()).routing || [];

        // Fetch device lists for all clusters in parallel.
        const deviceFetches = await Promise.all(
          clusterNames.map(c =>
            fetch(`${this.API}/devices?device_cluster=${encodeURIComponent(c)}`)
              .then(r => r.json()).then(d => d.devices || [])
          )
        );
        const allDevices = deviceFetches.flat();

        this.clusters = this.buildClusters(allDevices, statusList, routing);
        this.lastRefresh = 'Last updated: ' + new Date().toLocaleTimeString();
      } catch (e) {
        this.error = e.message;
      }
    },

    buildClusters(devices, statusList, routing) {
      // Look up status by "cluster/device_id".
      const statusMap = {};
      for (const s of statusList) {
        statusMap[`${s.device_cluster}/${s.device_id}`] = s;
      }
      // Routing entries keyed by edge "cluster/device_id" → target_device + updated_at.
      const routeByEdge = {};
      for (const r of routing) {
        routeByEdge[`${r.source_cluster}/${r.source_device}`] = r;
      }

      // Preserve _newImage/_hostPort across refreshes by reusing prior device objects.
      const priorById = {};
      for (const c of this.clusters) {
        for (const srv of c.servers) {
          priorById[srv.device.device_id] = srv.device;
          for (const e of srv.edges) priorById[e.device_id] = e;
        }
        for (const e of c.unassignedEdges) priorById[e.device_id] = e;
      }

      const decorate = (d) => {
        const key = `${d.device_cluster}/${d.device_id}`;
        const s = statusMap[key] || {};
        const route = routeByEdge[key];
        const prior = priorById[d.device_id] || {};
        return {
          ...d,
          current_model: s.current_model || null,
          status: s.status || null,
          last_heartbeat: s.last_heartbeat || null,
          _newImage: prior._newImage || '',
          _hostPort: prior._hostPort || '',
          _routeUpdated: route ? route.updated_at : null,
          _routeTarget: route ? route.target_device : null,
        };
      };

      // Group devices by cluster.
      const byCluster = {};
      for (const raw of devices) {
        const d = decorate(raw);
        (byCluster[d.device_cluster] ??= []).push(d);
      }

      const result = [];
      for (const [name, devs] of Object.entries(byCluster)) {
        const servers = devs.filter(d => d.device_type === 'server');
        const edges = devs.filter(d => d.device_type === 'edge');

        const serverCards = servers.map(srv => ({
          device: srv,
          edges: edges.filter(e => e._routeTarget === srv.device_id),
        }));
        const assigned = new Set();
        for (const sc of serverCards) for (const e of sc.edges) assigned.add(e.device_id);
        const unassignedEdges = edges.filter(e => !assigned.has(e.device_id));

        result.push({
          name,
          servers: serverCards,
          unassignedEdges,
          totalEdges: edges.length,
        });
      }
      result.sort((a, b) => a.name.localeCompare(b.name));
      return result;
    },

    badgeClass(status) {
      if (!status) return 'badge-unknown';
      const s = status.toLowerCase();
      if (s === 'active') return 'badge-active';
      if (s === 'unresponsive') return 'badge-unresponsive';
      if (s.includes('fail')) return 'badge-failure';
      return 'badge-unknown';
    },

    fmtTime(ts) {
      if (!ts) return '—';
      const d = new Date(ts.replace(' ', 'T') + 'Z');
      return isNaN(d) ? ts : d.toLocaleTimeString();
    },

    showToast(msg, ok = true) {
      this.toast = { visible: true, msg, ok };
      setTimeout(() => this.toast.visible = false, 3000);
    },

    async callDeploy(d, image, hostPort) {
      const body = {
        device_cluster: d.device_cluster,
        device_id: d.device_id,
        device_type: d.device_type,
        image,
      };
      if (hostPort) body.host_port = parseInt(hostPort);
      const res = await fetch(`${this.API}/deploy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      return data;
    },

    async deployFromRow(d) {
      const image = (d._newImage || '').trim();
      if (!image) { this.showToast('Enter a Docker image first.', false); return; }
      try {
        await this.callDeploy(d, image, d._hostPort || null);
        this.showToast(`Deployed ${image} to ${d.device_id}`);
        d._newImage = '';
        d._hostPort = '';
        setTimeout(() => this.refresh(), 1500);
      } catch (e) {
        this.showToast(`Deploy failed: ${e.message}`, false);
      }
    },

    async removeFromRow(d) {
      if (!confirm(`Remove container on ${d.device_cluster}/${d.device_id}?`)) return;
      try {
        const res = await fetch(`${this.API}/delete`, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device_cluster: d.device_cluster, device_id: d.device_id }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.statusText);
        this.showToast(`Container removed from ${d.device_id}`);
        setTimeout(() => this.refresh(), 1500);
      } catch (e) {
        this.showToast(`Remove failed: ${e.message}`, false);
      }
    },
  }
}
