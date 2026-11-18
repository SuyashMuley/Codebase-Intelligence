// k6 load script: a closed-loop (as fast as the server answers) mixed workload of signed-in users.
//   VUS=10 DURATION=30s k6 run load.js     (see loadtest/README.md for the whole procedure)
import http from "k6/http";
import { check } from "k6";
import { SharedArray } from "k6/data";
import { Counter } from "k6/metrics";

const fixture = JSON.parse(open("/fixture.json"));
const tokens = new SharedArray("tokens", () => fixture.tokens);
const BASE = __ENV.BASE || "http://host.docker.internal:8200";
const REPO = fixture.repo_id;
const bad = new Counter("bad_status");

export const options = {
  scenarios: {
    mixed: { executor: "constant-vus", vus: Number(__ENV.VUS || 10), duration: __ENV.DURATION || "30s" },
  },
  // Thresholds here only make k6 report per-endpoint sub-metrics; nothing is judged pass or fail.
  thresholds: Object.fromEntries(
    ["list", "status", "files", "file", "stats", "graph", "impact", "search_vector", "search_keyword", "chat"].map((n) => [
      `http_req_duration{name:${n}}`,
      ["max>=0"],
    ]),
  ),
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

// What a person does in the app, roughly: look around far more than they ask.
const MIX = [
  [20, "list"], [15, "status"], [10, "files"], [15, "file"], [5, "stats"],
  [5, "graph"], [5, "impact"], [12, "search_vector"], [5, "search_keyword"], [8, "chat"],
];
const total = MIX.reduce((a, [w]) => a + w, 0);
function choose() {
  let r = Math.random() * total;
  for (const [w, n] of MIX) { if ((r -= w) < 0) return n; }
  return MIX[0][1];
}

export default function () {
  const token = tokens[(__VU - 1) % tokens.length];
  const headers = { Cookie: `cip_session=${token}`, Origin: "http://localhost:3000", "Content-Type": "application/json" };
  const api = `${BASE}/api/repositories`;
  const name = choose();
  let res;
  switch (name) {
    case "list": res = http.get(`${api}?limit=20`, { headers, tags: { name } }); break;
    case "status": res = http.get(`${api}/${REPO}/status`, { headers, tags: { name } }); break;
    case "files": res = http.get(`${api}/${REPO}/files`, { headers, tags: { name } }); break;
    case "file": res = http.get(`${api}/${REPO}/files/${pick(fixture.paths)}`, { headers, tags: { name } }); break;
    case "stats": res = http.get(`${api}/${REPO}/stats`, { headers, tags: { name } }); break;
    case "graph": res = http.get(`${api}/${REPO}/graph`, { headers, tags: { name } }); break;
    case "impact": res = http.get(`${api}/${REPO}/impact?file=${encodeURIComponent(pick(fixture.paths))}`, { headers, tags: { name } }); break;
    case "search_vector":
      res = http.post(`${api}/${REPO}/search`, JSON.stringify({ query: pick(fixture.queries), mode: "vector", limit: 10 }), { headers, tags: { name } }); break;
    case "search_keyword":
      res = http.post(`${api}/${REPO}/search`, JSON.stringify({ query: pick(fixture.queries), mode: "keyword", limit: 10 }), { headers, tags: { name } }); break;
    case "chat":
      res = http.post(`${api}/${REPO}/chat`, JSON.stringify({ question: pick(fixture.queries) }), { headers, tags: { name } }); break;
  }
  const ok = check(res, { "status 200": (r) => r.status === 200 });
  if (!ok) bad.add(1, { endpoint: name, status: String(res.status) });
}
