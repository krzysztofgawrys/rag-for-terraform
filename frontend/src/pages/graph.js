import * as d3 from 'd3';
import { apiFetch } from '../api';
/**
 * Render a D3 force-directed dependency graph for the given module
 * inside the element with id="graphContainer".
 */
export async function renderDependencyGraph(module, onNavigate) {
    const container = document.getElementById('graphContainer');
    container.innerHTML = '<div class="placeholder-msg"><span class="icon">&#x21BB;</span><div>Loading graph...</div></div>';
    const nodesMap = new Map();
    const links = [];
    // Selected module as center node
    nodesMap.set(module.module_name, {
        id: module.module_name,
        name: module.module_name,
        repo: module.repo,
        type: 'selected',
    });
    // Fetch dependencies (what this module depends on)
    try {
        const depData = await apiFetch(`/modules/${encodeURIComponent(module.repo)}/${encodeURIComponent(module.module_path)}/dependencies${module.version ? `?version=${encodeURIComponent(module.version)}` : ''}`);
        for (const entry of depData.dependency_tree) {
            // chain is [root, ..., dep] — add each consecutive pair as a link
            for (let i = 0; i < entry.chain.length - 1; i++) {
                const src = entry.chain[i];
                const tgt = entry.chain[i + 1];
                if (!nodesMap.has(src)) {
                    nodesMap.set(src, { id: src, name: src, repo: '', type: 'dependency' });
                }
                if (!nodesMap.has(tgt)) {
                    nodesMap.set(tgt, { id: tgt, name: tgt, repo: entry.dep_repo || '', type: 'dependency' });
                }
                // Avoid duplicate links
                if (!links.some((l) => l.source === src && l.target === tgt)) {
                    links.push({ source: src, target: tgt });
                }
            }
        }
    }
    catch {
        // No dependencies — that's fine
    }
    // Fetch dependents (who depends on this module)
    try {
        const depData = await apiFetch(`/modules/${encodeURIComponent(module.repo)}/${encodeURIComponent(module.module_path)}/dependents${module.version ? `?version=${encodeURIComponent(module.version)}` : ''}`);
        for (const dep of depData.dependents) {
            if (!nodesMap.has(dep.name)) {
                nodesMap.set(dep.name, { id: dep.name, name: dep.name, repo: dep.repo, type: 'dependent' });
            }
            if (!links.some((l) => l.source === dep.name && l.target === module.module_name)) {
                links.push({ source: dep.name, target: module.module_name });
            }
        }
    }
    catch {
        // No dependents — that's fine
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
    svg
        .append('defs')
        .append('marker')
        .attr('id', 'arrowhead')
        .attr('viewBox', '0 -5 10 10')
        .attr('refX', 22)
        .attr('refY', 0)
        .attr('markerWidth', 6)
        .attr('markerHeight', 6)
        .attr('orient', 'auto')
        .append('path')
        .attr('d', 'M0,-5L10,0L0,5')
        .attr('fill', '#4a5568');
    const g = svg.append('g');
    // Zoom
    svg.call(d3.zoom()
        .scaleExtent([0.3, 3])
        .on('zoom', (event) => {
        g.attr('transform', event.transform);
    }));
    // Simulation
    const simulation = d3
        .forceSimulation(nodes)
        .force('link', d3.forceLink(links).id((d) => d.id).distance(100))
        .force('charge', d3.forceManyBody().strength(-300))
        .force('center', d3.forceCenter(width / 2, height / 2))
        .force('collision', d3.forceCollide().radius(30));
    // Links
    const link = g
        .selectAll('.graph-link')
        .data(links)
        .join('line')
        .attr('class', 'graph-link')
        .attr('marker-end', 'url(#arrowhead)');
    // Nodes
    const node = g
        .selectAll('.graph-node')
        .data(nodes)
        .join('g')
        .attr('class', 'graph-node')
        .style('cursor', 'pointer')
        .call(d3.drag()
        .on('start', (event, d) => {
        if (!event.active)
            simulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
    })
        .on('drag', (event, d) => {
        d.fx = event.x;
        d.fy = event.y;
    })
        .on('end', (event, d) => {
        if (!event.active)
            simulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
    }));
    // Node circles
    node
        .append('circle')
        .attr('r', (d) => (d.type === 'selected' ? 14 : 10))
        .attr('class', (d) => `graph-node-circle graph-node-${d.type}`);
    // Node labels
    node
        .append('text')
        .attr('class', 'graph-node-label')
        .attr('dy', -18)
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
    // Click node → navigate
    node.on('click', (_event, d) => {
        if (d.type !== 'selected') {
            onNavigate(d.name);
        }
    });
    // Tick
    simulation.on('tick', () => {
        link
            .attr('x1', (d) => d.source.x)
            .attr('y1', (d) => d.source.y)
            .attr('x2', (d) => d.target.x)
            .attr('y2', (d) => d.target.y);
        node.attr('transform', (d) => `translate(${d.x},${d.y})`);
    });
}
