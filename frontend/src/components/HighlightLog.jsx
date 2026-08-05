import { useConsoleState } from '../state/ConsoleState';

export default function HighlightLog() {
  const { highlightLog } = useConsoleState();
  if (!highlightLog.length) return null;

  return (
    <div className="mt-3 rounded-lg bg-gray-50 p-3">
      <h4 className="mb-2 text-xs font-medium text-gray-500">Recent highlights</h4>
      {highlightLog.map((entry, i) => (
        <div key={i} className="flex items-center gap-2 py-1 text-xs">
          <span className="font-mono text-gray-400">{new Date(entry.ts).toLocaleTimeString()}</span>
          <span className="text-green-600">{entry.status?.slice(0, 50)}</span>
        </div>
      ))}
    </div>
  );
}
