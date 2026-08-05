import { useState, useRef, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { getRobotById, getMissionsByRobot } from '../data/fleet';
import { useConsoleState } from '../state/ConsoleState';
import { apiURL } from '../api/client';
import RerunViewer from '../components/RerunViewer';

const ROBOT_ICONS = { 'Quadruped': '🐕', 'Wheeled': '🛞', 'Drone': '🚁' };
const ACTIONS = ['360° Photo Capture', 'Thermal Scan', 'LiDAR Scan', 'Gas Detection', 'Gap Measurement', 'Photo Capture'];
const CAT_COLORS = { 'Lights': '#fbbf24', 'Advertisement Board': '#3b82f6', 'Ticket Gate': '#a855f7', 'Map': '#22c55e', 'TV': '#6366f1', 'Exit Sign': '#ef4444' };

function PathDrawingMap({ robot, savedWaypoints, onWaypointsChange }) {
  const svgRef = useRef(null);
  const [viewBox, setViewBox] = useState({ x: -50, y: -10, w: 100, h: 70 });
  const [isDragging, setIsDragging] = useState(false);
  const [isDrawing, setIsDrawing] = useState(false);
  const [hoverPos, setHoverPos] = useState(null);
  const [selectedWp, setSelectedWp] = useState(null);
  const dragStart = useRef({});

  const svgToData = (e) => {
    const rect = svgRef.current.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const x = viewBox.x + (px / rect.width) * viewBox.w;
    const y = viewBox.y + (py / rect.height) * viewBox.h;
    return { x, y };
  };

  const onSvgMouseDown = (e) => {
    if (isDrawing) {
      const pos = svgToData(e);
      onWaypointsChange([...savedWaypoints, { x: pos.x, y: pos.y, action: ACTIONS[0] }]);
    } else {
      setIsDragging(true);
      const rect = svgRef.current.getBoundingClientRect();
      dragStart.current = { x: e.clientX, y: e.clientY, vbx: viewBox.x, vby: viewBox.y, rectW: rect.width, rectH: rect.height };
    }
  };

  const onSvgMouseMove = (e) => {
    const pos = svgToData(e);
    setHoverPos(pos);
    if (isDragging && !isDrawing) {
      const dx = e.clientX - dragStart.current.x;
      const dy = e.clientY - dragStart.current.y;
      const scaleX = viewBox.w / dragStart.current.rectW;
      const scaleY = viewBox.h / dragStart.current.rectH;
      setViewBox(prev => ({ ...prev, x: dragStart.current.vbx - dx * scaleX, y: dragStart.current.vby - dy * scaleY }));
    }
  };

  const onSvgMouseUp = () => setIsDragging(false);

  const handleWheel = (e) => {
    e.preventDefault();
    const scale = e.deltaY > 0 ? 1.1 : 0.9;
    setViewBox(prev => {
      const newW = prev.w * scale, newH = prev.h * scale;
      const cx = prev.x + prev.w / 2, cy = prev.y + prev.h / 2;
      return { x: cx - newW / 2, y: cy - newH / 2, w: newW, h: newH };
    });
  };

  const pathPoints = savedWaypoints.map(w => `${w.x},${w.y}`).join(' ');
  const robotX = robot?.pos?.x ?? 0;
  const robotY = robot?.pos?.y ?? 0;

  return (
    <div className="relative h-[500px] w-full overflow-hidden rounded-lg bg-gray-900" style={{ cursor: isDrawing ? 'crosshair' : isDragging ? 'grabbing' : 'grab' }}>
      <svg
        ref={svgRef}
        className="absolute inset-0 h-full w-full"
        viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
        onMouseDown={onSvgMouseDown}
        onMouseMove={onSvgMouseMove}
        onMouseUp={onSvgMouseUp}
        onMouseLeave={() => { onSvgMouseUp(); setHoverPos(null); }}
        onWheel={handleWheel}
      >
        <rect x="-100" y="-50" width="200" height="140" fill="#0f172a" />

        {/* Station layout */}
        <rect x="-45" y="0" width="35" height="6" fill="#1e3a5f" stroke="#3b5998" strokeWidth="0.2" rx="0.5" />
        <text x="-27" y="3.5" fill="#6080a0" fontSize="1.2" textAnchor="middle">PLATFORM</text>
        <rect x="-45" y="10" width="90" height="30" fill="#1a1a2e" stroke="#333" strokeWidth="0.12" rx="0.5" />
        <text x="0" y="28" fill="#475569" fontSize="1.5" textAnchor="middle">CONCOURSE LEVEL</text>
        <rect x="-44" y="44" width="8" height="4" fill="#1a2a1a" stroke="#22c55e" strokeWidth="0.2" rx="0.3" />
        <text x="-40" y="46.8" fill="#22c55e" fontSize="0.8" textAnchor="middle">EXIT A</text>
        <rect x="36" y="44" width="8" height="4" fill="#1a2a1a" stroke="#22c55e" strokeWidth="0.2" rx="0.3" />
        <text x="40" y="46.8" fill="#22c55e" fontSize="0.8" textAnchor="middle">EXIT B</text>

        {/* Grid */}
        {[-40, -20, 0, 20, 40].map(x => <line key={'v' + x} x1={x} y1="-10" x2={x} y2="60" stroke="#1e293b" strokeWidth="0.05" />)}
        {[-10, 0, 10, 20, 30, 40, 50].map(y => <line key={'h' + y} x1="-50" y1={y} x2="50" y2={y} stroke="#1e293b" strokeWidth="0.05" />)}

        {/* Robot current position */}
        <g>
          <circle cx={robotX} cy={robotY} r="1.5" fill="#10b981" stroke="#fff" strokeWidth="0.2" />
          <circle cx={robotX} cy={robotY} r="1.5" fill="none" stroke="#10b981" strokeWidth="0.15" opacity="0.5">
            <animate attributeName="r" from="1.5" to="5" dur="2s" repeatCount="indefinite" />
            <animate attributeName="opacity" from="0.6" to="0" dur="2s" repeatCount="indefinite" />
          </circle>
          <text x={robotX} y={robotY - 2.5} fill="#10b981" fontSize="1.2" textAnchor="middle" opacity="0.9">
            {ROBOT_ICONS[robot?.type]} {robot?.name}
          </text>
        </g>

        {/* Drawn path */}
        {savedWaypoints.length > 1 && (
          <polyline points={pathPoints} fill="none" stroke="#E3002C" strokeWidth="0.4" strokeDasharray="1 0.3" opacity="0.8" />
        )}

        {/* Waypoint markers */}
        {savedWaypoints.map((wp, i) => (
          <g key={i} onClick={(e) => { e.stopPropagation(); setSelectedWp(i); }} style={{ cursor: 'pointer' }}>
            <circle cx={wp.x} cy={wp.y} r={selectedWp === i ? 1.2 : 0.8} fill="#E3002C" stroke="#fff" strokeWidth="0.15" />
            <circle cx={wp.x} cy={wp.y} r="1.8" fill="none" stroke="#E3002C" strokeWidth="0.1" opacity="0.3" />
            <text x={wp.x} y={wp.y - 2} fill="#fff" fontSize="0.9" textAnchor="middle">{i + 1}</text>
          </g>
        ))}

        {/* Hover indicator when drawing */}
        {isDrawing && hoverPos && (
          <g>
            <circle cx={hoverPos.x} cy={hoverPos.y} r="0.6" fill="#E3002C" opacity="0.5" />
            <line x1={savedWaypoints.length > 0 ? savedWaypoints[savedWaypoints.length - 1].x : robotX}
                  y1={savedWaypoints.length > 0 ? savedWaypoints[savedWaypoints.length - 1].y : robotY}
                  x2={hoverPos.x} y2={hoverPos.y}
                  stroke="#E3002C" strokeWidth="0.2" strokeDasharray="0.5 0.3" opacity="0.5" />
          </g>
        )}

        {/* Compass */}
        <g transform="translate(45, -7)">
          <circle cx="0" cy="0" r="2.5" fill="none" stroke="#475569" strokeWidth="0.15" />
          <text x="0" y="-1.5" fill="#64748b" fontSize="1" textAnchor="middle">N</text>
        </g>
      </svg>

      {/* Toolbar */}
      <div className="absolute right-3 top-3 flex flex-col gap-1.5">
        <button onClick={() => setViewBox({ x: -50, y: -10, w: 100, h: 70 })} className="rounded bg-black/60 px-2 py-1 text-xs text-white backdrop-blur hover:bg-black/80">⟲ Reset</button>
        <button
          onClick={() => setIsDrawing(!isDrawing)}
          className={'rounded px-2 py-1 text-xs font-medium backdrop-blur ' + (isDrawing ? 'bg-[#E3002C] text-white' : 'bg-black/60 text-white hover:bg-black/80')}
        >
          {isDrawing ? '✋ Stop Drawing' : '✏️ Draw Path'}
        </button>
        {savedWaypoints.length > 0 && (
          <button onClick={() => onWaypointsChange([])} className="rounded bg-black/60 px-2 py-1 text-xs text-white backdrop-blur hover:bg-red-900">🗑 Clear</button>
        )}
      </div>

      {/* Info */}
      <div className="absolute left-3 top-3 rounded-lg bg-black/60 px-3 py-2 text-xs text-white backdrop-blur">
        <div className="font-semibold">{isDrawing ? 'Drawing Mode — Click to add waypoints' : 'Drag to pan · Scroll to zoom'}</div>
        {savedWaypoints.length > 0 && <div className="mt-1 text-white/60">{savedWaypoints.length} waypoint(s) drawn</div>}
      </div>
    </div>
  );
}

export default function RobotDetail() {
  const { id } = useParams();
  const robot = getRobotById(id);
  const robotMissions = getMissionsByRobot(id);
  const { db, addHighlightLog } = useConsoleState();
  const [waypoints, setWaypoints] = useState([]);
  const [missionName, setMissionName] = useState('');
  const [missionType, setMissionType] = useState('Custom Patrol');
  const [deployed, setDeployed] = useState(false);
  const [showRerun, setShowRerun] = useState(false);

  const rerunAddr = db?.rerun?.viewer_addr || '127.0.0.1:9876';
  const rerunLive = db?.rerun?.listening;

  if (!robot) return <div className="py-20 text-center text-gray-400">Robot not found</div>;

  const batteryColor = robot.battery > 50 ? 'text-green-600' : robot.battery > 20 ? 'text-amber-600' : 'text-red-600';
  const batteryBg = robot.battery > 50 ? 'bg-green-500' : robot.battery > 20 ? 'bg-amber-500' : 'bg-red-500';
  const stateColor = robot.state === 'active' ? 'bg-green-50 text-green-700' : robot.state === 'docked' ? 'bg-blue-50 text-blue-700' : robot.state === 'returning' ? 'bg-amber-50 text-amber-700' : 'bg-red-50 text-red-700';

  const handleActionChange = (wpIndex, action) => {
    setWaypoints(prev => prev.map((wp, i) => i === wpIndex ? { ...wp, action } : wp));
  };

  const handleDeploy = async () => {
    setDeployed(true);
    setShowRerun(true);
    try {
      // Plot the drawn path as waypoints + lines in Rerun
      // 2D map x → 3D x, 2D map y → 3D z, use robot's Y as ground level
      const robotY = robot.pos.y;
      const pathWaypoints = [
        { x: robot.pos.x, y: robot.pos.y, z: robot.pos.z, label: robot.name },
        ...waypoints.map((wp, i) => ({ x: wp.x, y: robotY, z: wp.y, label: `WP${i + 1}: ${wp.action}` })),
      ];
      await fetch(apiURL('/api/rerun/plot-path'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ waypoints: pathWaypoints, label: missionName || 'Custom Patrol' }),
      });
      addHighlightLog({ status: 'ok', action: 'deploy_mission', robot: robot.id, waypoints: waypoints.length });
    } catch (e) {
      addHighlightLog({ status: 'error', action: 'deploy' });
    }
  };

  const totalDistance = waypoints.reduce((sum, wp, i) => {
    if (i === 0) {
      const dx = wp.x - robot.pos.x, dy = wp.y - robot.pos.y;
      return sum + Math.sqrt(dx*dx + dy*dy);
    }
    const dx = wp.x - waypoints[i-1].x, dy = wp.y - waypoints[i-1].y;
    return sum + Math.sqrt(dx*dx + dy*dy);
  }, 0);

  return (
    <div className="mx-auto max-w-7xl px-6 py-8">
      {/* Header */}
      <div className="mb-6 flex items-start justify-between">
        <div>
          <Link to="/missions" className="mb-2 inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700">← Missions</Link>
          <h1 className="mt-1 flex items-center gap-3 text-2xl font-bold text-gray-900">
            <span className="text-3xl">{ROBOT_ICONS[robot.type]}</span>
            {robot.name}
          </h1>
          <div className="mt-2 flex items-center gap-3">
            <span className="font-mono text-sm text-gray-500">{robot.id}</span>
            <span className={'rounded px-2 py-0.5 text-xs font-medium ' + stateColor}>{robot.state}</span>
            <span className="text-sm text-gray-500">{robot.type}</span>
            <span className="text-sm text-gray-500">· {robot.station}</span>
          </div>
        </div>
        <div className="text-right">
          <div className={'text-3xl font-bold ' + batteryColor}>{robot.battery}%</div>
          <div className="mt-1 h-2 w-32 rounded-full bg-gray-100">
            <div className={'h-2 rounded-full ' + batteryBg} style={{ width: robot.battery + '%' }} />
          </div>
          <div className="mt-1 text-xs text-gray-400">Battery</div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-4">
        {/* Left: Telemetry */}
        <div className="space-y-4">
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-700">Telemetry</h2>
            <div className="space-y-3 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-500">State</span>
                <span className="font-medium text-gray-900">{robot.state}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Station</span>
                <span className="font-medium text-gray-900">{robot.station}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Temperature</span>
                <span className="font-mono font-medium text-gray-900">{robot.temp}°C</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Signal RSSI</span>
                <span className="font-mono font-medium text-gray-900">{robot.signal != null ? robot.signal + ' dBm' : '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Position</span>
                <span className="font-mono text-xs text-gray-600">[{robot.pos.x}, {robot.pos.y}, {robot.pos.z}]</span>
              </div>
              {robot.mission && (
                <div className="border-t border-gray-100 pt-3">
                  <div className="text-xs text-gray-500">Current Mission</div>
                  <div className="mt-1 text-sm font-medium text-gray-900">{robot.mission}</div>
                </div>
              )}
            </div>
          </div>

          {/* Mission history */}
          {robotMissions.length > 0 && (
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-700">Mission History</h2>
              <div className="space-y-2">
                {robotMissions.map(m => (
                  <div key={m.id} className="rounded-lg border border-gray-100 p-3">
                    <div className="text-sm font-medium text-gray-900">{m.name}</div>
                    <div className="mt-1 flex items-center justify-between text-xs text-gray-500">
                      <span>{m.window}</span>
                      <span className="font-medium text-gray-700">{m.progress}%</span>
                    </div>
                    <div className="mt-1 h-1.5 rounded-full bg-gray-100">
                      <div className="h-1.5 rounded-full bg-[#E3002C]" style={{ width: m.progress + '%' }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Center: Path drawing map + waypoint config */}
        <div className="lg:col-span-3 space-y-4">
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-700">Draw Custom Patrol Path</h2>
              <div className="flex items-center gap-2 text-xs text-gray-500">
                <span>Click "Draw Path" then click on map to add waypoints</span>
              </div>
            </div>
            <PathDrawingMap robot={robot} savedWaypoints={waypoints} onWaypointsChange={setWaypoints} />
          </div>

          {/* Mission configuration */}
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-700">Mission Configuration</h2>
            <div className="mb-4 grid grid-cols-1 gap-4 md:grid-cols-2">
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-500">Mission Name</label>
                <input
                  type="text"
                  value={missionName}
                  onChange={e => setMissionName(e.target.value)}
                  placeholder={`Custom Patrol — ${robot.station}`}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#E3002C] focus:outline-none focus:ring-2 focus:ring-[#E3002C]/20"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-500">Mission Type</label>
                <select
                  value={missionType}
                  onChange={e => setMissionType(e.target.value)}
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#E3002C] focus:outline-none"
                >
                  <option>Custom Patrol</option>
                  <option>Trackbed Inspection</option>
                  <option>Facility Check</option>
                  <option>Platform Gap Inspection</option>
                  <option>Thermal Scan</option>
                </select>
              </div>
            </div>

            {/* Waypoint list */}
            {waypoints.length > 0 && (
              <div className="mb-4">
                <div className="mb-2 flex items-center justify-between">
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-700">Waypoints ({waypoints.length})</h3>
                  <div className="text-xs text-gray-500">Est. distance: {totalDistance.toFixed(1)}m</div>
                </div>
                <div className="space-y-2">
                  {waypoints.map((wp, i) => (
                    <div key={i} className="flex items-center gap-3 rounded-lg border border-gray-100 p-3">
                      <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full bg-[#E3002C] text-xs font-bold text-white">
                        {i + 1}
                      </div>
                      <div className="flex-1">
                        <div className="font-mono text-xs text-gray-500">[{wp.x.toFixed(1)}, {wp.y.toFixed(1)}]</div>
                      </div>
                      <select
                        value={wp.action}
                        onChange={e => handleActionChange(i, e.target.value)}
                        className="rounded-lg border border-gray-300 px-2 py-1 text-xs focus:border-[#E3002C] focus:outline-none"
                      >
                        {ACTIONS.map(a => <option key={a} value={a}>{a}</option>)}
                      </select>
                      <button
                        onClick={() => setWaypoints(prev => prev.filter((_, idx) => idx !== i))}
                        className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-600"
                      >
                        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Deploy button */}
            <div className="flex items-center gap-3">
              <button
                onClick={handleDeploy}
                disabled={waypoints.length === 0 || deployed}
                className="rounded-lg bg-[#E3002C] px-6 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-[#c2001f] disabled:opacity-40"
              >
                {deployed ? '✓ Mission Deployed' : 'Deploy Mission to Robot'}
              </button>
              {waypoints.length === 0 && <span className="text-xs text-gray-400">Draw at least one waypoint</span>}
              {deployed && <span className="text-xs text-green-600">Mission sent to {robot.name}</span>}
            </div>
          </div>

          {/* Embedded Rerun 3D viewer showing the plotted path */}
          {showRerun && rerunLive && (
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-700">3D Path Preview — Rerun Viewer</h2>
                <span className="flex items-center gap-1.5 text-xs text-green-600">
                  <span className="h-2 w-2 rounded-full bg-green-500 animate-pulse" /> Connected
                </span>
              </div>
              <RerunViewer addr={rerunAddr} height="400px" className="overflow-hidden rounded-lg border border-gray-200" />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}