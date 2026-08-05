import { NavLink } from 'react-router-dom';
import { useConsoleState } from '../state/ConsoleState';
import RerunStatusChip from './RerunStatusChip';

const NAV = [
  { to: '/overview', label: 'Overview', icon: '📊' },
  { to: '/missions', label: 'Missions', icon: '🤖' },
  { to: '/defects', label: 'Defects', icon: '⚠️' },
  { to: '/analytics', label: 'Analytics', icon: '📈' },
  { to: '/digital-twin', label: 'Twin', icon: '🗺️' },
  { to: '/categories', label: 'Categories', icon: '📦' },
  { to: '/search', label: 'Search', icon: '🔍' },
  { to: '/assistant', label: 'Assistant', icon: '🤖' },
  { to: '/voice', label: 'Voice', icon: '🎙️' },
];

export default function TopBar() {
  const { db, inspectionId, setInspectionId } = useConsoleState();

  return (
    <header className="sticky top-0 z-50 flex h-14 items-center justify-between bg-[#E3002C] px-6 text-white shadow-lg">
      <div className="flex items-center gap-3">
        <div className="flex h-7 w-7 items-center justify-center rounded bg-white text-sm font-extrabold text-[#E3002C]">M</div>
        <span className="text-sm font-bold tracking-wide">MTR-Insight</span>
      </div>

      <nav className="flex items-center gap-1">
        {NAV.map(n => (
          <NavLink
            key={n.to}
            to={n.to}
            className={({ isActive }) =>
              `flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors ${
                isActive
                  ? 'bg-white/20 text-white'
                  : 'text-white/75 hover:bg-white/10 hover:text-white'
              }`
            }
          >
            <span className="text-base">{n.icon}</span>
            <span className="hidden sm:inline">{n.label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="flex items-center gap-3">
        {db.inspections?.length > 1 && (
          <select
            className="rounded-md border border-white/20 bg-white/10 px-2 py-1 text-xs text-white placeholder-white/50 focus:outline-none focus:ring-2 focus:ring-white/30"
            value={inspectionId ?? ''}
            onChange={e => setInspectionId(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">All inspections</option>
            {db.inspections.map(ins => (
              <option key={ins.id} value={ins.id} className="text-black">
                #{ins.id} — {ins.started_at}{ins.is_gt ? ' (GT)' : ''}
              </option>
            ))}
          </select>
        )}
        <RerunStatusChip />
      </div>
    </header>
  );
}