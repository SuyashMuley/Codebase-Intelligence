// Loaded only in the browser (via next/dynamic with ssr: false). Bundles Monaco with just the
// languages this app indexes, instead of loading the whole editor from a third-party CDN, so the
// viewer works offline and no source code or visitor IP is sent to anyone else.
import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/editor/editor.api";
import "monaco-editor/languages/definitions/go/register";
import "monaco-editor/languages/definitions/javascript/register";
import "monaco-editor/languages/definitions/python/register";
import "monaco-editor/languages/definitions/typescript/register";

(self as unknown as { MonacoEnvironment: monaco.Environment }).MonacoEnvironment = {
  getWorker: () =>
    new Worker(new URL("monaco-editor/editor/editor.worker.js", import.meta.url), {
      type: "module",
    }),
};

loader.config({ monaco });

export { monaco };
