"use client";

import "@xyflow/react/dist/style.css";
import { Background, Controls, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";
import { useMemo } from "react";
import { layoutGraph, NODE_HEIGHT, NODE_WIDTH } from "@/lib/graph-layout";
import type { GraphData, GraphNodeData } from "@/lib/types";

const COLORS = {
  imports: "#34d399",
  calls: "#60a5fa",
  unresolved: "#f59e0b",
};

function nodeStyle(n: GraphNodeData, selected: boolean): React.CSSProperties {
  const base: React.CSSProperties = {
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    fontSize: 12,
    padding: "0 8px",
    borderRadius: 6,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    color: "#e4e4e7",
    background: "#18181b",
    border: "1px solid #3f3f46",
  };
  if (n.kind === "external") {
    return { ...base, border: "1px dashed #b45309", color: "#fcd34d", background: "#1c1917" };
  }
  if (n.kind === "symbol") base.background = "#1e293b";
  if (n.focus) base.border = "1px solid #52525b";
  if (selected) {
    base.border = "2px solid #34d399";
    base.background = "#064e3b";
  }
  return base;
}

export function GraphCanvas({
  graph,
  selectedId,
  onSelect,
}: {
  graph: GraphData;
  selectedId: string | null;
  onSelect: (node: GraphNodeData) => void;
}) {
  const { nodes, edges } = useMemo(() => {
    const positions = layoutGraph(graph.nodes, graph.edges);
    const byId = new Map(graph.nodes.map((n) => [n.id, n]));
    const nodes: Node[] = graph.nodes.map((n) => ({
      id: n.id,
      position: positions.get(n.id) ?? { x: 0, y: 0 },
      data: { label: n.label },
      style: nodeStyle(n, n.id === selectedId),
      draggable: false,
      ariaLabel: `${n.kind} ${n.path ?? n.label}`,
    }));
    const edges: Edge[] = graph.edges
      .filter((e) => byId.has(e.source) && byId.has(e.target))
      .map((e, i) => {
        const color = e.resolution === "unresolved" ? COLORS.unresolved : COLORS[e.kind];
        return {
          id: `${e.source}>${e.target}:${e.kind}:${i}`,
          source: e.source,
          target: e.target,
          markerEnd: { type: MarkerType.ArrowClosed, color },
          style: {
            stroke: color,
            strokeWidth: 1.5,
            strokeDasharray: e.resolution === "unresolved" ? "5 4" : undefined,
          },
        };
      });
    return { nodes, edges };
  }, [graph, selectedId]);

  return (
    <div
      className="h-[60vh] min-h-80 rounded-lg border border-zinc-800 bg-zinc-950"
      data-testid="graph-canvas"
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        forceColorMode="dark"
        fitView
        minZoom={0.1}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_e, node) => {
          const data = graph.nodes.find((n) => n.id === node.id);
          if (data) onSelect(data);
        }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

export function GraphLegend() {
  const item = (color: string, label: string, dashed = false) => (
    <li className="flex items-center gap-2">
      <svg width="28" height="8" aria-hidden>
        <line
          x1="0"
          y1="4"
          x2="28"
          y2="4"
          stroke={color}
          strokeWidth="2"
          strokeDasharray={dashed ? "5 4" : undefined}
        />
      </svg>
      {label}
    </li>
  );
  return (
    <ul aria-label="Legend" className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-zinc-400">
      {item(COLORS.imports, "imports (static)")}
      {item(COLORS.calls, "calls (static)")}
      {item(COLORS.unresolved, "call that could not be resolved", true)}
      <li>An arrow from A to B means A depends on B.</li>
    </ul>
  );
}
