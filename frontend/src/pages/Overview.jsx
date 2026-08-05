import { Link } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import { useConsoleState } from '../state/ConsoleState';
import { getRobots, getMissions } from '../data/fleet';

const ANOMALY_SEVERITY = {
  'crack and structure damage': { level: 'critical', color: 'bg-red-100 text-red-700 ring-red-600/20', dot: 'bg-red-500' },
  'foreign_object': { level: 'high', color: 'bg-orange-100 text-orange-700 ring-orange-600/20', dot: 'bg-orange-500' },
  'missing_object': { level: 'high', color: 'bg-orange-100 text-orange-700 ring-orange-600/20', dot: 'bg-orange-500' },
  'state_change': { level: 'medium', color: 'bg-amber-100 text-amber-700 ring-amber-600/20', dot: 'bg-amber-500' },
  'content_change': { level: 'low', color: 'bg-blue-100 text-blue-700 ring-blue-600/20', dot: 'bg-blue-500' },
  'stain/graffiti': { level: 'low', color: 'bg-blue-100 text-blue-700 ring-blue-600/20', dot: 'bg-blue-500' },
  'relocation': { level: 'medium', color: 'bg-amber-100 text-amber-700 ring-amber-600/20', dot: 'bg-amber-500' },
};

function severityFor(type) {
  return ANOMALY_SEVERITY[type] || { level: 'unknown', color: 'bg-gray-100 text-gray-700 ring-gray-600/20', dot: 'bg-gray-500' };
}

export default function Overview() {
  const { db } = useConsoleState();
  const { data: anomalySummary } = useApi('/api/anomalies/summary');
  const { data: anomalies } = useApi('/api/anomalies?limit=all');
  const { data: anomalyLocations } = useApi('/api/anomalies/locations');

  const inspections = db?.inspections || [];
  const totalObjects = inspections.reduce((s, i) => s + (i.object_count || 0), 0);
  const totalDetections = inspections.reduce((s, i) => s + (i.detection_count || 0), 0);
  const totalAnomalies = anomalySummary?.total_abnormalities || 0;

  const criticalAnomalies = (anomalies || []).filter(a => {
    const sev = severityFor(a.type).level;
    return sev === 'critical' || sev === 'high';
  });

  const robots = getRobots();
  const missions = getMissions();
  const activeRobots = robots.filter(r => r.state === 'active').length;
  const dockedRobots = robots.filter(r => r.state === 'docked').length;
  const offlineRobots = robots.filter(r => r.state === 'offline').length;
  const returningRobots = robots.filter(r => r.state === 'returning').length;
  const activeMissions = missions.filter(m => m.status === 'in_progress').length;

  return (
    <div className="mx-auto max-w-7xl px-4 py-4">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Station Inspection Overview</h1>
          <p className="text-xs text-gray-500">Real-time situational awareness across all inspection missions</p>
        </div>
        <Link to="/analytics" className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50">📊 Analytics</Link>
      </div>

      {/* Fleet Status Summary Bar - compact */}
      <div className="mb-4 grid grid-cols-4 gap-2 md:grid-cols-8">
        <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-sm">
          <div className="text-xl font-bold text-gray-900">{inspections.length}</div>
          <div className="text-[10px] text-gray-500">Inspections</div>
        </div>
        <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-sm">
          <div className="text-xl font-bold text-blue-600">{totalObjects}</div>
          <div className="text-[10px] text-gray-500">Objects</div>
        </div>
        <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-sm">
          <div className="text-xl font-bold text-green-600">{totalDetections}</div>
          <div className="text-[10px] text-gray-500">Detections</div>
        </div>
        <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-sm">
          <div className="text-xl font-bold text-amber-600">{totalAnomalies}</div>
          <div className="text-[10px] text-gray-500">Anomalies</div>
        </div>
        <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-sm">
          <div className="text-xl font-bold text-red-600">{criticalAnomalies.length}</div>
          <div className="text-[10px] text-gray-500">Critical</div>
        </div>
        <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-sm">
          <div className="text-xl font-bold text-indigo-600">{activeRobots}</div>
          <div className="text-[10px] text-gray-500">Robots</div>
        </div>
        <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-sm">
          <div className="text-xl font-bold text-purple-600">{activeMissions}</div>
          <div className="text-[10px] text-gray-500">Missions</div>
        </div>
        <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-sm">
          <div className="flex items-center gap-1.5">
            <div className={'h-2 w-2 rounded-full ' + (db?.rerun?.listening ? 'bg-green-500' : 'bg-gray-400')} />
            <div className="text-xs font-bold text-gray-900">{db?.rerun?.listening ? 'Live' : 'Off'}</div>
          </div>
          <div className="text-[10px] text-gray-500">3D Viewer</div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* Left: Inspection missions */}
        <div className="lg:col-span-2 space-y-4">
          {/* Active Inspection Missions */}
          <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-700">Inspection Missions</h2>
              <Link to="/digital-twin" className="text-xs font-medium text-[#E3002C] hover:underline">View 3D →</Link>
            </div>
            <div className="space-y-2">
              {inspections.map(ins => {
                const insAnomalies = (anomalies || []).filter(a => a.inspection_id === ins.id);
                const gtLabel = ins.is_gt ? 'Ground Truth' : 'Live Inspection';
                return (
                  <Link
                    key={ins.id}
                    to={'/inspection/' + ins.id}
                    className="block rounded-lg border border-gray-100 p-3 transition-all hover:border-[#E3002C]/30 hover:shadow-sm"
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <div className={'flex h-8 w-8 items-center justify-center rounded-lg text-xs font-bold ' + (ins.is_gt ? 'bg-green-50 text-green-600' : 'bg-blue-50 text-blue-600')}>
                          #{ins.id}
                        </div>
                        <div>
                          <div className="text-sm font-semibold text-gray-900">{gtLabel}</div>
                          <div className="text-[10px] text-gray-500">{ins.started_at}</div>
                        </div>
                      </div>
                      <div className="flex items-center gap-3 text-xs">
                        <div className="text-center">
                          <div className="font-bold text-gray-900">{ins.object_count}</div>
                          <div className="text-[9px] text-gray-400">objects</div>
                        </div>
                        <div className="text-center">
                          <div className="font-bold text-gray-900">{ins.detection_count}</div>
                          <div className="text-gray-400">detections</div>
                        </div>
                        {!ins.is_gt && (
                          <div className="text-center">
                            <div className={`font-bold ${insAnomalies.length > 0 ? 'text-amber-600' : 'text-green-600'}`}>
                              {insAnomalies.length}
                            </div>
                            <div className="text-gray-400">anomalies</div>
                          </div>
                        )}
                      </div>
                    </div>
                  </Link>
                );
              })}
              {!inspections.length && <p className="py-4 text-center text-sm text-gray-400">No inspections found</p>}
            </div>
          </div>

          {/* Station Risk Heatmap - anomaly locations */}
          {anomalyLocations?.length > 0 && (
            <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
              <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-700">Anomaly Location Map</h2>
              <div className="relative h-64 overflow-hidden rounded-lg bg-gray-900">
                <svg className="absolute inset-0 h-full w-full" viewBox="-50 -5 100 60">
                  <rect x="-50" y="-5" width="100" height="60" fill="#1a1a2e" />
                  {anomalyLocations.map((loc, i) => {
                    const x = loc.cam_x;
                    const y = loc.cam_z;
                    const sev = severityFor(loc.type);
                    return (
                      <g key={i}>
                        <circle
                          cx={x} cy={y} r="2.5"
                          fill={sev.level === 'critical' ? '#ef4444' : sev.level === 'high' ? '#f97316' : sev.level === 'medium' ? '#f59e0b' : '#3b82f6'}
                          opacity="0.8"
                        >
                          <title>{loc.object}: {loc.type}</title>
                        </circle>
                        <circle cx={x} cy={y} r="2.5" fill="none" stroke={sev.level === 'critical' ? '#ef4444' : '#f97316'} strokeWidth="0.3" opacity="0.4">
                          <animate attributeName="r" from="2.5" to="6" dur="2s" repeatCount="indefinite" />
                          <animate attributeName="opacity" from="0.6" to="0" dur="2s" repeatCount="indefinite" />
                        </circle>
                      </g>
                    );
                  })}
                  <text x="-48" y="2" fill="#666" fontSize="2">Station Anomaly Heatmap (X-Z plane)</text>
                </svg>
              </div>
              <div className="mt-3 flex flex-wrap gap-3 text-xs">
                <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-red-500" /> Critical</span>
                <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-orange-500" /> High</span>
                <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-amber-500" /> Medium</span>
                <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-blue-500" /> Low</span>
              </div>
            </div>
          )}
        </div>

        {/* Right: Fleet & Alerts side-by-side */}
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
          {/* Fleet Status Panel */}
          <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-700">Fleet & Robots</h2>
              <Link to="/missions" className="text-[10px] font-medium text-[#E3002C] hover:underline">→</Link>
            </div>
            <div className="mb-2 flex flex-wrap items-center gap-2 text-[10px]">
              <span className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-green-500" />{activeRobots}</span>
              <span className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-blue-500" />{dockedRobots}</span>
              <span className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-amber-500" />{returningRobots}</span>
              {offlineRobots > 0 && <span className="flex items-center gap-1"><span className="h-1.5 w-1.5 rounded-full bg-red-500" />{offlineRobots}</span>}
            </div>
            <div className="space-y-1.5">
              {robots.map(robot => {
                const batteryColor = robot.battery > 50 ? 'text-green-600' : robot.battery > 20 ? 'text-amber-600' : 'text-red-600';
                const batteryBg = robot.battery > 50 ? 'bg-green-500' : robot.battery > 20 ? 'bg-amber-500' : 'bg-red-500';
                const stateColor = robot.state === 'active' ? 'bg-green-50 text-green-700' : robot.state === 'docked' ? 'bg-blue-50 text-blue-700' : robot.state === 'returning' ? 'bg-amber-50 text-amber-700' : 'bg-red-50 text-red-700';
                return (
                  <Link key={robot.id} to="/missions" className="block rounded-lg border border-gray-100 p-2 transition-colors hover:border-[#E3002C]/30 hover:bg-red-50/30">
                    <div className="flex items-center justify-between gap-1">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1">
                          <span className="truncate text-xs font-semibold text-gray-900">{robot.name}</span>
                          <span className={'rounded px-1 py-0.5 text-[8px] font-medium ' + stateColor}>{robot.state}</span>
                        </div>
                        <div className="mt-0.5 truncate text-[10px] text-gray-500">{robot.station}</div>
                        {robot.mission && <div className="mt-0.5 truncate text-[10px] text-gray-400">→ {robot.mission}</div>}
                      </div>
                      <div className="flex flex-shrink-0 flex-col items-end gap-0.5">
                        <div className={'text-xs font-bold ' + batteryColor}>{robot.battery}%</div>
                        <div className="h-1 w-12 rounded-full bg-gray-100">
                          <div className={'h-1 rounded-full ' + batteryBg} style={{ width: robot.battery + '%' }} />
                        </div>
                        {robot.completion > 0 && (
                          <div className="text-[8px] text-gray-400">{robot.completion}%</div>
                        )}
                      </div>
                    </div>
                  </Link>
                );
              })}
            </div>
          </div>

          {/* Critical Alerts Feed */}
          <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-gray-700">Critical Alerts</h2>
              <Link to="/defects" className="text-[10px] font-medium text-[#E3002C] hover:underline">→</Link>
            </div>
            <div className="space-y-1.5">
              {(anomalies || []).slice(0, 6).map(a => {
                const sev = severityFor(a.type);
                return (
                  <Link
                    key={a.id}
                    to={'/defects?focus=' + a.id}
                    className="block rounded-lg border-l-4 bg-gray-50 p-2 transition-colors hover:bg-gray-100"
                    style={{ borderLeftColor: sev.level === 'critical' ? '#ef4444' : sev.level === 'high' ? '#f97316' : '#f59e0b' }}
                  >
                    <div className="flex items-start justify-between gap-1">
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-xs font-medium text-gray-900">{a.object || 'Unknown'}</div>
                        <div className="mt-0.5 truncate text-[10px] text-gray-500">{a.location || a.type}</div>
                      </div>
                      <span className={'inline-flex flex-shrink-0 items-center rounded-full px-1.5 py-0.5 text-[8px] font-medium ring-1 ring-inset ' + sev.color}>
                        {sev.level}
                      </span>
                    </div>
                  </Link>
                );
              })}
              {!anomalies?.length && <p className="py-4 text-center text-xs text-gray-400">No alerts</p>}
            </div>
          </div>
          </div>

          {/* Quick Actions */}
          <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-700">Quick Actions</h2>
            <div className="grid grid-cols-3 gap-2">
              <Link to="/defects" className="flex items-center justify-center gap-1.5 rounded-lg bg-[#E3002C] px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-[#c2001f]">
                Defects ({totalAnomalies})
              </Link>
              <Link to="/digital-twin" className="flex items-center justify-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs font-semibold text-gray-700 transition-colors hover:bg-gray-50">
                3D Twin
              </Link>
              <Link to="/assistant" className="flex items-center justify-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs font-semibold text-gray-700 transition-colors hover:bg-gray-50">
                AI Assistant
              </Link>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}