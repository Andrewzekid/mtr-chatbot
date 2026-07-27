import { useState } from "react";

/**
 * Displays the database tool calls the LLM router selected for the current / latest turn,
 * including the raw output returned by each tool and the raw tool-calling model response.
 *
 * @param {{ toolCalls: Array<{ requestId?: string, calls: Array<{ name: string, args: object, output?: string|object }> }>, toolRouterRaw: { requestId?: string, raw: object }|null }} props
 */
export default function DebugPanel({ toolCalls, toolRouterRaw }) {
  const [expanded, setExpanded] = useState(true);

  if ((!toolCalls || toolCalls.length === 0) && !toolRouterRaw) {
    return null;
  }

  return (
    <section className="card debug-panel">
      <div className="debug-header">
        <h2>Debug: Tool Calls</h2>
        <button
          type="button"
          className="debug-toggle"
          onClick={() => setExpanded((prev) => !prev)}
          aria-label={expanded ? "Collapse" : "Expand"}
        >
          {expanded ? "−" : "+"}
        </button>
      </div>
      {expanded && (
        <div className="debug-body">
          {toolRouterRaw && (
            <div className="debug-turn">
              <p className="debug-turn-label">
                Tool-calling model raw response
                {toolRouterRaw.requestId ? ` · ${toolRouterRaw.requestId.slice(0, 8)}` : ""}
              </p>
              <details className="debug-call-output" open>
                <summary>Raw response</summary>
                <pre>{JSON.stringify(toolRouterRaw.raw, null, 2)}</pre>
              </details>
            </div>
          )}
          {toolCalls.map((turn, idx) => (
            <div key={turn.requestId || idx} className="debug-turn">
              <p className="debug-turn-label">
                Turn {idx + 1}
                {turn.requestId ? ` · ${turn.requestId.slice(0, 8)}` : ""}
              </p>
              <ul className="debug-call-list">
                {turn.calls.map((call, cidx) => (
                  <li key={cidx} className="debug-call">
                    <code className="debug-call-name">{call.name}</code>
                    <pre className="debug-call-args">
                      {JSON.stringify(call.args, null, 2)}
                    </pre>
                    {call.output && (
                      <details className="debug-call-output">
                        <summary>Raw output</summary>
                        <pre>
                          {typeof call.output === "string"
                            ? call.output
                            : JSON.stringify(call.output, null, 2)}
                        </pre>
                      </details>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
