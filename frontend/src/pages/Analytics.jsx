import { useApi } from '../hooks/useApi';
import { useConsoleState } from '../state/ConsoleState';
import { getRobots, getMissions } from '../data/fleet';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug'];

// Generate mock historical data for the past month
function generateMonthlyData() {
  const inspectionsPerWeek = [3, 5, 4, 6, 5, 7, 4, 5];
  const anomaliesPerWeek = [2, 4, 1, 5, 3, 6, 2, 3];
  const assetsPerWeek = [120, 135, 128, 142, 138, 150, 130, 145];
  const detectionsPerWeek = [380, 420, 410, 460, 440, 480, 400, 450];
  return { inspectionsPerWeek, anomaliesPerWeek, assetsPerWeek, detectionsPerWeek };
}

function StatCard({ label, value, sublabel, color, icon }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between">
        <div className="text-xs text-gray-500">{label}</div>
        <div className="text-lg">{icon}</div>
      </div>
      <div className={'mt-2 text-2xl font-bold ' + (color || 'text-gray-900')}>{value}</div>
      {sublabel && <div className="mt-1 text-[10px] text-gray-400">{sublabel}</div>}
    </div>
  );
}

function BarChart({ title, data, labels, color, unit }) {
  const max = Math.max(...data, 1);
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-700">{title}</h3>
      <div className="flex items-end gap-2" style={{ height: '120px' }}>
        {data.map((v, i) => (
          <div key={i} className="flex flex-1 flex-col items-center gap-1">
            <div className="text-[9px] font-medium text-gray-600">{v}</div>
            <div
              className="w-full rounded-t transition-all hover:opacity-80"
              style={{
                height: (v / max) * 80 + '%',
                background: color,
                minHeight: '4px',
              }}
            />
            <div className="text-[9px] text-gray-400">{labels[i]}</div>
          </div>
        ))}
      </div>
      {unit && <div className="mt-2 text-center text-[10px] text-gray-400">{unit}</div>}
    </div>
  );
}

function TrendChart({ title, data, labels, color }) {
  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const range = max - min || 1;
  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * 100;
    const y = 100 - ((v - min) / range) * 90 - 5;
    return x + ',' + y;
  }).join(' ');

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-700">{title}</h3>
      <div className="relative">
        <svg className="w-full" viewBox="0 0 100 100" preserveAspectRatio="none" style={{ height: '120px' }}>
          <polyline points={points} fill="none" stroke={color} strokeWidth="1.5" />
          {data.map((v, i) => {
            const x = (i / (data.length - 1)) * 100;
            const y = 100 - ((v - min) / range) * 90 - 5;
            return <circle key={i} cx={x} cy={y} r="1.5" fill={color} />;
          })}
        </svg>
      </div>
      <div className="mt-1 flex justify-between text-[9px] text-gray-400">
        {labels.map((l, i) => <span key={i}>{l}</span>)}
      </div>
    </div>
  );
}

export default function Analytics() {
  const { db } = useConsoleState();
  const { data: anomalies } = useApi('/api/anomalies?limit=all');
  const { data: anomalySummary } = useApi('/api/anomalies/summary');
  const { data: categories } = useApi('/api/categories');
  const robots = getRobots();
  const missions = getMissions();

  const monthly = generateMonthlyData();
  const inspections = db?.inspections || [];
  const totalObjects = inspections.reduce((s, i) => s + (i.object_count || 0), 0);
  const totalDetections = inspections.reduce((s, i) => s + (i.detection_count || 0), 0);

  // Anomaly type breakdown
  const anomalyTypes = (anomalySummary?.by_type || []).sort((a, b) => b.count - a.count);
  const totalAnomalies = anomalySummary?.total_abnormalities || 0;

  // Category breakdown
  const catData = (categories || []).sort((a, b) => b.count - a.count);
  const maxCat = Math.max(...catData.map(c => c.count), 1);

  // Monthly totals
  const monthInspections = monthly.inspectionsPerWeek.reduce((a, b) => a + b, 0);
  const monthAnomalies = monthly.anomaliesPerWeek.reduce((a, b) => a + b, 0);
  const monthAssets = monthly.assetsPerWeek.reduce((a, b) => a + b, 0);
  const monthDetections = monthly.detectionsPerWeek.reduce((a, b) => a + b, 0);

  // Completion rate
  const completedMissions = missions.filter(m => m.status === 'completed').length;
  const activeMissions = missions.filter(m => m.status === 'in_progress').length;
  const avgProgress = missions.reduce((s, m) => s + m.progress, 0) / (missions.length || 1);

  // Robot utilization
  const activeRobots = robots.filter(r => r.state === 'active').length;
  const avgBattery = robots.reduce((s, r) => s + r.battery, 0) / (robots.length || 1);

  const weekLabels = ['W1', 'W2', 'W3', 'W4', 'W5', 'W6', 'W7', 'W8'];

  return (
    <div className="mx-auto max-w-7xl px-4 py-4">
      <div className="mb-4">
        <h1 className="text-xl font-bold text-gray-900">Analytics & Trends</h1>
        <p className="text-xs text-gray-500">Inspection statistics over the past month</p>
      </div>

      {/* Top stat cards */}
      <div className="mb-4 grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-8">
        <StatCard label="Inspections" value={monthInspections} sublabel="this month" color="text-gray-900" icon="🔍" />
        <StatCard label="Anomalies" value={monthAnomalies} sublabel="detected" color="text-amber-600" icon="⚠️" />
        <StatCard label="Assets Located" value={monthAssets} sublabel="total" color="text-blue-600" icon="📦" />
        <StatCard label="Detections" value={monthDetections} sublabel="total" color="text-green-600" icon="📸" />
        <StatCard label="Categories" value={catData.length} sublabel="active" color="text-purple-600" icon="🏷️" />
        <StatCard label="Avg Progress" value={avgProgress.toFixed(0) + '%'} sublabel="all missions" color="text-indigo-600" icon="📊" />
        <StatCard label="Robots Active" value={activeRobots + '/' + robots.length} sublabel="deployed" color="text-cyan-600" icon="🤖" />
        <StatCard label="Avg Battery" value={avgBattery.toFixed(0) + '%'} sublabel="fleet" color="text-pink-600" icon="🔋" />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* Weekly inspections */}
        <BarChart title="Inspections per Week" data={monthly.inspectionsPerWeek} labels={weekLabels} color="#3b82f6" unit="Number of inspection missions completed" />

        {/* Weekly anomalies */}
        <BarChart title="Anomalies Found per Week" data={monthly.anomaliesPerWeek} labels={weekLabels} color="#f59e0b" unit="AI-detected anomalies requiring review" />

        {/* Assets detected trend */}
        <TrendChart title="Assets Located (Trend)" data={monthly.assetsPerWeek} labels={weekLabels} color="#10b981" />

        {/* Detections trend */}
        <TrendChart title="Total Detections (Trend)" data={monthly.detectionsPerWeek} labels={weekLabels} color="#8b5cf6" />
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* Anomaly type breakdown */}
        <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
          <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-700">Anomaly Types Breakdown</h3>
          <div className="space-y-2">
            {anomalyTypes.map(t => {
              const pct = (t.count / totalAnomalies) * 100;
              return (
                <div key={t.type} className="flex items-center gap-2">
                  <div className="w-32 truncate text-xs text-gray-600">{t.type}</div>
                  <div className="flex-1">
                    <div className="h-4 rounded bg-gray-100">
                      <div className="h-4 rounded bg-amber-500" style={{ width: pct + '%' }} />
                    </div>
                  </div>
                  <div className="w-8 text-right text-xs font-bold text-gray-700">{t.count}</div>
                </div>
              );
            })}
            {!anomalyTypes.length && <p className="py-4 text-center text-sm text-gray-400">No anomaly data</p>}
          </div>
        </div>

        {/* Category distribution */}
        <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
          <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-700">Asset Category Distribution</h3>
          <div className="space-y-2">
            {catData.map(c => {
              const pct = (c.count / maxCat) * 100;
              return (
                <div key={c.category} className="flex items-center gap-2">
                  <div className="w-32 truncate text-xs text-gray-600">{c.category}</div>
                  <div className="flex-1">
                    <div className="h-4 rounded bg-gray-100">
                      <div className="h-4 rounded bg-blue-500" style={{ width: pct + '%' }} />
                    </div>
                  </div>
                  <div className="w-8 text-right text-xs font-bold text-gray-700">{c.count}</div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Mission completion stats */}
        <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
          <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-700">Mission Status</h3>
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-xs text-gray-500">Completed</span>
              <span className="text-sm font-bold text-green-600">{completedMissions}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-gray-500">In Progress</span>
              <span className="text-sm font-bold text-blue-600">{activeMissions}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-gray-500">Scheduled</span>
              <span className="text-sm font-bold text-purple-600">{missions.filter(m => m.status === 'scheduled').length}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-gray-500">Returning</span>
              <span className="text-sm font-bold text-amber-600">{missions.filter(m => m.status === 'returning').length}</span>
            </div>
            <div className="border-t border-gray-100 pt-3">
              <div className="flex items-center justify-between">
                <span className="text-xs text-gray-500">Total Missions</span>
                <span className="text-sm font-bold text-gray-900">{missions.length}</span>
              </div>
              <div className="mt-2 flex items-center justify-between">
                <span className="text-xs text-gray-500">Completion Rate</span>
                <span className="text-sm font-bold text-gray-900">{((completedMissions / missions.length) * 100).toFixed(0)}%</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Robot fleet stats */}
      <div className="mt-4 rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-700">Robot Fleet Utilization</h3>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-5">
          {robots.map(r => {
            const rMissions = missions.filter(m => m.robotId === r.id);
            const rCompleted = rMissions.filter(m => m.status === 'completed').length;
            const rAnomalies = rMissions.reduce((s, m) => s + m.anomalies, 0);
            return (
              <div key={r.id} className="rounded-lg border border-gray-100 p-3">
                <div className="flex items-center gap-2">
                  <span className="text-lg">{r.type === 'Quadruped' ? '🐕' : r.type === 'Wheeled' ? '🛞' : '🚁'}</span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs font-semibold text-gray-900">{r.name}</div>
                    <div className="truncate text-[10px] text-gray-500">{r.id}</div>
                  </div>
                </div>
                <div className="mt-2 space-y-1 text-[10px]">
                  <div className="flex justify-between"><span className="text-gray-500">Missions</span><span className="font-medium text-gray-700">{rMissions.length}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Completed</span><span className="font-medium text-green-600">{rCompleted}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Anomalies Found</span><span className="font-medium text-amber-600">{rAnomalies}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Battery</span><span className={'font-medium ' + (r.battery > 50 ? 'text-green-600' : r.battery > 20 ? 'text-amber-600' : 'text-red-600')}>{r.battery}%</span></div>
                  <div className="mt-1 h-1 rounded-full bg-gray-100">
                    <div className={'h-1 rounded-full ' + (r.battery > 50 ? 'bg-green-500' : r.battery > 20 ? 'bg-amber-500' : 'bg-red-500')} style={{ width: r.battery + '%' }} />
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}