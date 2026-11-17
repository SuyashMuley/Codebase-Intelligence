import dagre from "@dagrejs/dagre";

export const NODE_WIDTH = 190;
export const NODE_HEIGHT = 38;

export interface LayoutNode {
  id: string;
}
export interface LayoutEdge {
  source: string;
  target: string;
}

/**
 * Left-to-right layered layout: a file sits to the left of what it depends on. Pure and
 * deterministic for a given input (dagre has no randomness), so the same graph always looks
 * the same.
 */
export function layoutGraph(
  nodes: LayoutNode[],
  edges: LayoutEdge[],
): Map<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 14, ranksep: 70, marginx: 10, marginy: 10 });
  g.setDefaultEdgeLabel(() => ({}));
  const ids = new Set(nodes.map((n) => n.id));
  for (const n of nodes) g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  for (const e of edges) {
    // An edge to a node that is not drawn (cut by the size cap) must not create a ghost node.
    if (ids.has(e.source) && ids.has(e.target) && e.source !== e.target) {
      g.setEdge(e.source, e.target);
    }
  }
  dagre.layout(g);
  const out = new Map<string, { x: number; y: number }>();
  for (const n of nodes) {
    const p = g.node(n.id);
    // dagre reports centres; React Flow positions the top-left corner.
    out.set(n.id, { x: p.x - NODE_WIDTH / 2, y: p.y - NODE_HEIGHT / 2 });
  }
  return out;
}
