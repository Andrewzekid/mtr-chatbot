import { useConsoleState } from '../state/ConsoleState';

export default function RerunStatusChip() {
  const { db } = useConsoleState();
  const st = db?.rerun || {};
  const color = st.listening ? 'bg-green-400' : st.enabled ? 'bg-amber-400' : 'bg-gray-400';
  const label = st.listening ? '3D Live' : st.enabled ? '3D Ready' : '3D Off';

  return (
    <div className="flex items-center gap-1.5 rounded-full bg-white/15 px-2.5 py-1 text-[11px] font-medium text-white">
      <span className={`h-2 w-2 rounded-full ${color}`} />
      {label}
    </div>
  );
}
