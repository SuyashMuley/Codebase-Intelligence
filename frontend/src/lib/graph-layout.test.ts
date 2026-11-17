import { describe, expect, it } from "vitest";
import { layoutGraph, NODE_WIDTH } from "./graph-layout";

describe("layoutGraph", () => {
  it("places a node to the left of what it depends on", () => {
    const pos = layoutGraph(
      [{ id: "a" }, { id: "b" }, { id: "c" }],
      [
        { source: "a", target: "b" },
        { source: "b", target: "c" },
      ],
    );
    expect(pos.get("a")!.x).toBeLessThan(pos.get("b")!.x);
    expect(pos.get("b")!.x).toBeLessThan(pos.get("c")!.x);
  });

  it("gives every node a position, including unconnected ones, without overlap", () => {
    const pos = layoutGraph([{ id: "a" }, { id: "b" }, { id: "lonely" }], [{ source: "a", target: "b" }]);
    expect([...pos.keys()].sort()).toEqual(["a", "b", "lonely"]);
    const boxes = [...pos.values()];
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const apart = Math.abs(boxes[i].x - boxes[j].x) >= NODE_WIDTH || Math.abs(boxes[i].y - boxes[j].y) >= 38;
        expect(apart).toBe(true);
      }
    }
  });

  it("ignores edges to nodes that are not drawn and self loops", () => {
    const pos = layoutGraph([{ id: "a" }], [
      { source: "a", target: "gone" },
      { source: "a", target: "a" },
    ]);
    expect([...pos.keys()]).toEqual(["a"]);
  });

  it("survives cycles and is deterministic", () => {
    const nodes = [{ id: "a" }, { id: "b" }, { id: "c" }];
    const edges = [
      { source: "a", target: "b" },
      { source: "b", target: "c" },
      { source: "c", target: "a" },
    ];
    expect([...layoutGraph(nodes, edges)]).toEqual([...layoutGraph(nodes, edges)]);
  });

  it("handles an empty graph", () => {
    expect(layoutGraph([], []).size).toBe(0);
  });
});
