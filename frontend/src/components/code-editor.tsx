"use client";

import Editor, { type OnMount } from "@monaco-editor/react";
import { useEffect, useRef } from "react";
import "./monaco-setup";
import { Spinner } from "./ui";

const MONACO_LANGUAGE: Record<string, string> = {
  python: "python",
  javascript: "javascript",
  typescript: "typescript",
  go: "go",
};

export interface CodeEditorProps {
  path: string;
  content: string;
  language: string | null;
  highlight: { start: number; end: number } | null;
}

type Editor = Parameters<OnMount>[0];

/** A read-only Monaco editor that can scroll to and highlight a range of lines. */
export default function CodeEditor({ path, content, language, highlight }: CodeEditorProps) {
  const editorRef = useRef<Editor | null>(null);
  const decorations = useRef<{ clear: () => void } | null>(null);

  function apply(editor: Editor, range: CodeEditorProps["highlight"]) {
    decorations.current?.clear();
    decorations.current = null;
    if (!range) return;
    const last = editor.getModel()?.getLineCount() ?? range.end;
    const start = Math.min(range.start, last);
    const end = Math.min(range.end, last);
    decorations.current = editor.createDecorationsCollection([
      {
        range: { startLineNumber: start, startColumn: 1, endLineNumber: end, endColumn: 1 },
        options: {
          isWholeLine: true,
          className: "cip-highlight",
          linesDecorationsClassName: "cip-highlight-gutter",
        },
      },
    ]);
    editor.revealLineInCenter(start);
  }

  useEffect(() => {
    if (editorRef.current) apply(editorRef.current, highlight);
  }, [highlight, path]);

  return (
    <Editor
      path={path}
      value={content}
      language={MONACO_LANGUAGE[language ?? ""] ?? "plaintext"}
      theme="vs-dark"
      loading={<Spinner label="Loading editor…" />}
      onMount={(editor) => {
        editorRef.current = editor;
        apply(editor, highlight);
      }}
      options={{
        readOnly: true,
        domReadOnly: true,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        automaticLayout: true,
        fontSize: 13,
        lineNumbersMinChars: 4,
        renderLineHighlight: "none",
        contextmenu: false,
        stickyScroll: { enabled: false },
        padding: { top: 8, bottom: 8 },
      }}
    />
  );
}
