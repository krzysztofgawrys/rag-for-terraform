import * as d3 from 'd3';
import { apiFetch } from '../api';
import type { Module } from '../types';

interface GraphNode extends d3.SimulationNodeDatum {
  id: string;
  name: string;
  repo: string;
  path: string;
  version: string;
  type: 'selected' | 'dependency' | 'dependent' | 'resource';
}

interface GraphLink extends d3.SimulationLinkDatum<GraphNode> {
  source: string | GraphNode;
  target: string | GraphNode;
}

interface DepTreeEntry {
  chain: string[];          // repo//path keys (unique IDs)
  chain_names: string[];    // human-readable names
  chain_versions: string[]; // version per chain element
  dep_name: string;
  dep_repo: string;
  dep_path: string;
}

interface DependentEntry {
  name: string;
  repo: string;
  path: string;
  version?: string;
}

/**
 * Render a D3 force-directed dependency graph for the given module
 * inside the element with id="graphContainer".
 */
export async function renderDependencyGraph(
  module: Module,
  onNavigate: (repo: string, path: string, version?: string) => void,
  depth: number = 1,
  moduleLookup?: (repo: string, path: string) => Module | undefined,
): Promise<void> {
  const container = document.getElementById('graphContainer')!;
  container.innerHTML = '<div class="placeholder-msg"><span class="icon">&#x21BB;</span><div>Loading graph...</div></div>';

  const nodesMap = new Map<string, GraphNode>();
  const links: GraphLink[] = [];

  // Helper: parse "repo//path" key into {repo, path}
  function parseKey(key: string): { repo: string; path: string } {
    const idx = key.indexOf('//');
    return idx >= 0
      ? { repo: key.slice(0, idx), path: key.slice(idx + 2) }
      : { repo: '', path: key };
  }

  // Selected module as center node
  const selectedKey = `${module.repo}//${module.module_path}`;
  nodesMap.set(selectedKey, {
    id: selectedKey,
    name: module.module_name,
    repo: module.repo,
    path: module.module_path,
    version: module.version || '',
    type: 'selected',
  });

  // Fetch dependencies (what this module depends on)
  try {
    const depData = await apiFetch<{ dependency_tree: DepTreeEntry[] }>(
      `/modules/${encodeURIComponent(module.repo)}/${encodeURIComponent(module.module_path)}/dependencies?depth=${depth}${module.version ? `&version=${encodeURIComponent(module.version)}` : ''}`,
    );

    for (const entry of depData.dependency_tree) {
      const { chain, chain_names, chain_versions } = entry;
      for (let i = 0; i < chain.length - 1; i++) {
        const srcKey = chain[i];
        const tgtKey = chain[i + 1];
        const srcName = chain_names[i] || srcKey;
        const tgtName = chain_names[i + 1] || tgtKey;
        const srcVer = chain_versions?.[i] || '';
        const tgtVer = chain_versions?.[i + 1] || '';
        if (!nodesMap.has(srcKey)) {
          const info = parseKey(srcKey);
          nodesMap.set(srcKey, { id: srcKey, name: srcName, repo: info.repo, path: info.path, version: srcVer, type: 'dependency' });
        }
        if (!nodesMap.has(tgtKey)) {
          const info = parseKey(tgtKey);
          nodesMap.set(tgtKey, { id: tgtKey, name: tgtName, repo: info.repo, path: info.path, version: tgtVer, type: 'dependency' });
        }
        if (!links.some((l) => l.source === srcKey && l.target === tgtKey)) {
          links.push({ source: srcKey, target: tgtKey });
        }
      }
    }
  } catch {
    // No dependencies — that's fine
  }

  // Fetch dependents (who depends on this module)
  try {
    const depData = await apiFetch<{ dependents: DependentEntry[] }>(
      `/modules/${encodeURIComponent(module.repo)}/${encodeURIComponent(module.module_path)}/dependents?depth=${depth}${module.version ? `&version=${encodeURIComponent(module.version)}` : ''}`,
    );
    for (const dep of depData.dependents) {
      const depKey = `${dep.repo}//${dep.path}`;
      const parts = dep.path ? dep.path.split('/') : [dep.name];
      const label = parts.length > 1 ? parts.slice(-2).join('/') : parts[0];
      if (!nodesMap.has(depKey)) {
        nodesMap.set(depKey, { id: depKey, name: label, repo: dep.repo, path: dep.path, version: dep.version || '', type: 'dependent' });
      }
      if (!links.some((l) => l.source === depKey && l.target === selectedKey)) {
        links.push({ source: depKey, target: selectedKey });
      }
    }
  } catch {
    // No dependents — that's fine
  }

  // Add AWS resources as leaf nodes
  if (depth <= 1) {
    // Direct mode: resources only for root
    if (module.resources?.length) {
      for (const res of module.resources) {
        const resKey = `resource::${selectedKey}::${res}`;
        if (!nodesMap.has(resKey)) {
          nodesMap.set(resKey, { id: resKey, name: res, repo: '', path: '', version: '', type: 'resource' });
          links.push({ source: selectedKey, target: resKey });
        }
      }
    }
  } else if (moduleLookup) {
    // Full chain: add resources for root + dependencies only (not dependents)
    const moduleNodes = Array.from(nodesMap.values()).filter(
      (n) => (n.type === 'selected' || n.type === 'dependency') && n.repo && n.path,
    );
    for (const n of moduleNodes) {
      const mod = moduleLookup(n.repo, n.path);
      const resources = mod?.resources || [];
      for (const res of resources) {
        const resKey = `resource::${n.id}::${res}`;
        if (!nodesMap.has(resKey)) {
          nodesMap.set(resKey, { id: resKey, name: res, repo: '', path: '', version: '', type: 'resource' });
          links.push({ source: n.id, target: resKey });
        }
      }
    }
  }

  const nodes = Array.from(nodesMap.values());

  if (nodes.length <= 1 && links.length === 0) {
    container.innerHTML =
      '<div class="placeholder-msg"><span class="icon">&#x2B21;</span><div>No dependencies found for this module</div></div>';
    return;
  }

  // Render
  container.innerHTML = '';
  const rect = container.getBoundingClientRect();
  const width = rect.width || 500;
  const height = rect.height || 400;

  const svg = d3
    .select(container)
    .append('svg')
    .attr('width', '100%')
    .attr('height', '100%')
    .attr('viewBox', `0 0 ${width} ${height}`);

  // Arrow marker
  const defs = svg.append('defs');
  defs.append('marker')
    .attr('id', 'arrowhead')
    .attr('viewBox', '0 -5 10 10')
    .attr('refX', 5)
    .attr('refY', 0)
    .attr('markerWidth', 7)
    .attr('markerHeight', 7)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M0,-5L10,0L0,5')
    .attr('fill', '#4a5568');

  const g = svg.append('g');

  // Zoom
  const zoom = d3.zoom<SVGSVGElement, unknown>()
    .scaleExtent([0.1, 3])
    .on('zoom', (event) => {
      g.attr('transform', event.transform);
    });
  svg.call(zoom);

  // Compute degree per node — high-degree nodes need more space around them
  const degree = new Map<string, number>();
  for (const l of links) {
    const s = typeof l.source === 'string' ? l.source : l.source.id;
    const t = typeof l.target === 'string' ? l.target : l.target.id;
    degree.set(s, (degree.get(s) || 0) + 1);
    degree.set(t, (degree.get(t) || 0) + 1);
  }

  // Simulation — tuned to minimise edge crossings
  const simulation = d3
    .forceSimulation<GraphNode>(nodes)
    .force('link', d3.forceLink<GraphNode, GraphLink>(links).id((d) => d.id).distance((l) => {
      const tgt = l.target as GraphNode;
      if (tgt.type === 'resource') return 70;
      // High-degree targets get longer links so their subtrees don't overlap
      const deg = degree.get(tgt.id) || 1;
      return 140 + Math.min(deg * 8, 80);
    }))
    .force('charge', d3.forceManyBody<GraphNode>()
      .strength((d) => {
        // Stronger repulsion for high-degree nodes — keeps their neighbours apart
        const deg = degree.get(d.id) || 1;
        return -500 - Math.min(deg * 40, 600);
      })
      .distanceMax(800))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('x', d3.forceX(width / 2).strength(0.02))
    .force('y', d3.forceY(height / 2).strength(0.08))
    .force('collision', d3.forceCollide<GraphNode>().radius((d) =>
      d.type === 'resource' ? 30 : 60,
    ))
    .alphaDecay(0.015)   // run simulation longer for better convergence
    .velocityDecay(0.5); // less jitter at rest

  // Links — use <path> with 3 points so marker-mid places arrowhead at center
  const link = g
    .selectAll<SVGPathElement, GraphLink>('.graph-link')
    .data(links)
    .join('path')
    .attr('class', 'graph-link')
    .attr('fill', 'none')
    .attr('marker-mid', 'url(#arrowhead)');

  // Nodes
  const node = g
    .selectAll<SVGGElement, GraphNode>('.graph-node')
    .data(nodes)
    .join('g')
    .attr('class', 'graph-node')
    .style('cursor', 'pointer')
    .call(
      d3.drag<SVGGElement, GraphNode>()
        .on('start', (event, d) => {
          if (!event.active) simulation.alphaTarget(0.3).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on('drag', (event, d) => {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on('end', (event, d) => {
          if (!event.active) simulation.alphaTarget(0);
          d.fx = null;
          d.fy = null;
        }),
    );

  // Node circles
  node
    .append('circle')
    .attr('r', (d) => (d.type === 'selected' ? 14 : d.type === 'resource' ? 6 : 10))
    .attr('class', (d) => `graph-node-circle graph-node-${d.type}`);

  // Node labels
  node
    .append('text')
    .attr('class', (d) => `graph-node-label${d.type === 'resource' ? ' graph-resource-label' : ''}`)
    .attr('dy', (d) => d.type === 'resource' ? -12 : -18)
    .attr('text-anchor', 'middle')
    .text((d) => d.name);

  // Tooltip on hover
  const tooltip = d3
    .select(container)
    .append('div')
    .attr('class', 'graph-tooltip');

  node
    .on('mouseenter', (_event, d) => {
      tooltip
        .style('opacity', '1')
        .html(`<strong>${d.name}</strong>${d.repo ? `<br>${d.repo}` : ''}<br><span style="color:var(--text3)">${d.type}</span>`);
    })
    .on('mousemove', (event) => {
      const containerRect = container.getBoundingClientRect();
      tooltip
        .style('left', event.clientX - containerRect.left + 12 + 'px')
        .style('top', event.clientY - containerRect.top - 10 + 'px');
    })
    .on('mouseleave', () => {
      tooltip.style('opacity', '0');
    });

  // Click node → navigate (not for selected or resource nodes)
  node
    .style('cursor', (d) => (d.type === 'resource' || d.type === 'selected') ? 'default' : 'pointer')
    .on('click', (_event, d) => {
      if (d.type !== 'selected' && d.type !== 'resource' && d.repo && d.path) {
        onNavigate(d.repo, d.path, d.version || undefined);
      }
    });

  // Fit graph to container
  function fitToView() {
    const pad = 60;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      if (n.x != null && n.y != null) {
        minX = Math.min(minX, n.x);
        minY = Math.min(minY, n.y);
        maxX = Math.max(maxX, n.x);
        maxY = Math.max(maxY, n.y);
      }
    }
    if (!isFinite(minX)) return;

    const graphW = maxX - minX + pad * 2;
    const graphH = maxY - minY + pad * 2;
    const currentRect = container.getBoundingClientRect();
    const viewW = currentRect.width || width;
    const viewH = currentRect.height || height;
    const scale = Math.min(viewW / graphW, viewH / graphH, 1.5);
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const tx = viewW / 2 - cx * scale;
    const ty = viewH / 2 - cy * scale;

    svg.transition().duration(500).call(
      zoom.transform,
      d3.zoomIdentity.translate(tx, ty).scale(scale),
    );
  }

  // Tick
  simulation.on('tick', () => {
    link.attr('d', (d) => {
      const s = d.source as GraphNode, t = d.target as GraphNode;
      const mx = (s.x! + t.x!) / 2, my = (s.y! + t.y!) / 2;
      return `M${s.x},${s.y} L${mx},${my} L${t.x},${t.y}`;
    });
    node.attr('transform', (d) => `translate(${d.x},${d.y})`);
  });

  // Auto-fit once simulation settles
  simulation.on('end', fitToView);
}
