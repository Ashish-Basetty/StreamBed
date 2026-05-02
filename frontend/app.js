function app() {
  return {
    API: 'http://localhost:8080',
    clusterNames: [],
    selectedCluster: null,
    cluster: null,
    query: '',
    showDropdown: false,
    error: null,
    lastRefresh: '',
    toast: { visible: false, msg: '', ok: true },

    async init() {
      const params = new URLSearchParams(window.location.search);
      this.selectedCluster = params.get('cluster');
      await this.loadClusterNames();
      if (this.selectedCluster) {
        this.query = this.selectedCluster;
        await this.refresh();
        setInterval(() => this.refresh(), 10000);
      }
    },

    async loadClusterNames() {
      try {
        const res = await fetch(`${this.API}/clusters`);
        if (!res.ok) throw new Error('clusters: ' + (await res.text()));
        this.clusterNames = ((await res.json()).clusters || []).sort();
      } catch (e) {
        this.error = e.message;
      }
    },

    get filteredClusters() {
      const q = this.query.trim().toLowerCase();
      if (!q) return this.clusterNames;
      return this.clusterNames.filter(n => n.toLowerCase().startsWith(q));
    },

    selectCluster(name) {
      if (!name) return;
      window.location.search = '?cluster=' + encodeURIComponent(name);
    },

    submitSearch() {
      const matches = this.filteredClusters;
      const target = this.clusterNames.includes(this.query.trim())
        ? this.query.trim()
        : matches[0];
      if (target) this.selectCluster(target);
    },

    clearCluster() {
      window.location.search = '';
    },

    async refresh() {
      if (!this.selectedCluster) return;
      this.error = null;
      try {
        const [statusRes, routingRes, devicesRes] = await Promise.all([
          fetch(`${this.API}/status`),
          fetch(`${this.API}/routing`),
          fetch(`${this.API}/devices?device_cluster=${encodeURIComponent(this.selectedCluster)}`),
        ]);
        if (!statusRes.ok) throw new Error('status: ' + (await statusRes.text()));
        if (!routingRes.ok) throw new Error('routing: ' + (await routingRes.text()));
        if (!devicesRes.ok) throw new Error('devices: ' + (await devicesRes.text()));
        const statusList = ((await statusRes.json()).status || [])
          .filter(s => s.device_cluster === this.selectedCluster);
        const routing = ((await routingRes.json()).routing || [])
          .filter(r => r.source_cluster === this.selectedCluster);
        const devices = (await devicesRes.json()).devices || [];

        this.cluster = this.buildCluster(this.selectedCluster, devices, statusList, routing);
        this.lastRefresh = 'Last updated: ' + new Date().toLocaleTimeString();
      } catch (e) {
        this.error = e.message;
      }
    },

    buildCluster(name, devices, statusList, routing) {
      const statusMap = {};
      for (const s of statusList) {
        statusMap[`${s.device_cluster}/${s.device_id}`] = s;
      }
      const routeByEdge = {};
      for (const r of routing) {
        routeByEdge[`${r.source_cluster}/${r.source_device}`] = r;
      }

      const priorById = {};
      if (this.cluster) {
        for (const srv of this.cluster.servers) {
          priorById[srv.device.device_id] = srv.device;
          for (const e of srv.edges) priorById[e.device_id] = e;
        }
        for (const e of this.cluster.unassignedEdges) priorById[e.device_id] = e;
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

      const decorated = devices.map(decorate);
      const servers = decorated.filter(d => d.device_type === 'server');
      const edges = decorated.filter(d => d.device_type === 'edge');

      const serverCards = servers.map(srv => ({
        device: srv,
        edges: edges.filter(e => e._routeTarget === srv.device_id),
      }));
      const assigned = new Set();
      for (const sc of serverCards) for (const e of sc.edges) assigned.add(e.device_id);
      const unassignedEdges = edges.filter(e => !assigned.has(e.device_id));

      return {
        name,
        servers: serverCards,
        unassignedEdges,
        totalEdges: edges.length,
      };
    },

    hasModel(d) {
      return !!(d && d.current_model && String(d.current_model).trim() !== '');
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
