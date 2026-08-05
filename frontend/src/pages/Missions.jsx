import { useState, useRef, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { getRobots, getMissions, getMissionById } from '../data/fleet';
import { useConsoleState } from '../state/ConsoleState';
import { apiURL } from '../api/client';
import RerunViewer from '../components/RerunViewer';

const ROBOT_ICONS = {
  'Quadruped': '🐕',
  'Wheeled': '🛞',
  'Drone': '🚁',
};

const STATE_COLORS = {
  'active': 'bg-green-50 text-green-700',
  'docked': 'bg-blue-50 text-blue-700',
  'returning': 'bg-amber-50 text-amber-700',
  'offline': 'bg-red-50 text-red-700',
};

const MISSION_STATUS_COLORS = {
  'in_progress': 'bg-green-50 text-green-700',
  'scheduled': 'bg-blue-50 text-blue-700',
  'completed': 'bg-gray-50 text-gray-500',
  'returning': 'bg-amber-50 text-amber-700',
};

function MissionsMap({ missions, robots, selectedMission, onSelectMission }) {
  const svgRef = useRef(null);
  const [viewBox, setViewBox] = useState({ x: -50, y: -10, w: 100, h: 70 });
  const [isDragging, setIsDragging] = useState(false);
  const dragStart = useRef({});

  const onMouseDown = (e) => {
    setIsDragging(true);
    const rect = svgRef.current.getBoundingClientRect();
    dragStart.current = { x: e.clientX, y: e.clientY, vbx: viewBox.x, vby: viewBox.y, rectW: rect.width, rectH: rect.height };
  };
  const onMouseMove = (e) => {
    if (!isDragging) return;
    const dx = e.clientX - dragStart.current.x;
    const dy = e.clientY - dragStart.current.y;
    const scaleX = viewBox.w / dragStart.current.rectW;
    const scaleY = viewBox.h / dragStart.current.rectH;
    setViewBox(prev => ({ ...prev, x: dragStart.current.vbx - dx * scaleX, y: dragStart.current.vby - dy * scaleY }));
  };
  const onMouseUp = () => setIsDragging(false);
  const handleWheel = (e) => {
    e.preventDefault();
    const scale = e.deltaY > 0 ? 1.1 : 0.9;
    setViewBox(prev => {
      const newW = prev.w * scale, newH = prev.h * scale;
      const cx = prev.x + prev.w / 2, cy = prev.y + prev.h / 2;
      return { x: cx - newW / 2, y: cy - newH / 2, w: newW, h: newH };
    });
  };

  const selMission = selectedMission ? getMissionById(selectedMission) : null;

  return (
    <div className="relative h-[500px] w-full overflow-hidden rounded-lg bg-gray-900" style={{ cursor: isDragging ? 'grabbing' : 'grab' }}>
      <svg
        ref={svgRef}
        className="absolute inset-0 h-full w-full"
        viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp}
        onMouseLeave={onMouseUp}
        onWheel={handleWheel}
      >
        <rect x="-100" y="-50" width="200" height="140" fill="#0f172a" />

        {/* Fake station layout - multiple stations */}
        {/* Station 1: Kowloon Tong */}
        <rect x="-45" y="0" width="35" height="6" fill="#1e3a5f" stroke="#3b5998" strokeWidth="0.2" rx="0.5" />
        <text x="-27" y="3.5" fill="#6080a0" fontSize="1.2" textAnchor="middle">KOWLOON TONG</text>
        <rect x="-45" y="10" width="35" height="20" fill="#1a1a2e" stroke="#333" strokeWidth="0.12" rx="0.5" />

        {/* Station 2: Central */}
        <rect x="-5" y="0" width="35" height="6" fill="#1e3a5f" stroke="#3b5998" strokeWidth="0.2" rx="0.5" />
        <text x="12" y="3.5" fill="#6080a0" fontSize="1.2" textAnchor="middle">CENTRAL</text>
        <rect x="-5" y="10" width="35" height="20" fill="#1a1a2e" stroke="#333" strokeWidth="0.12" rx="0.5" />

        {/* Station 3: Admiralty (tunnel) */}
        <rect x="15" y="-8" width="30" height="4" fill="#1a2a3a" stroke="#3b5998" strokeWidth="0.15" rx="0.3" />
        <text x="30" y="-5.5" fill="#6080a0" fontSize="1" textAnchor="middle">ADMIRALTY (TUNNEL)</text>

        {/* Station 4: Mong Kok */}
        <rect x="-35" y="35" width="25" height="5" fill="#1e3a5f" stroke="#3b5998" strokeWidth="0.2" rx="0.5" />
        <text x="-22.5" y="38" fill="#6080a0" fontSize="1" textAnchor="middle">MONG KOK</text>
        <rect x="-35" y="42" width="25" height="15" fill="#1a1a2e" stroke="#333" strokeWidth="0.12" rx="0.5" />

        {/* Station 5: Prince Edward */}
        <rect x="5" y="35" width="25" height="5" fill="#1e3a5f" stroke="#3b5998" strokeWidth="0.2" rx="0.5" />
        <text x="17.5" y="38" fill="#6080a0" fontSize="1" textAnchor="middle">PRINCE EDWARD</text>
        <rect x="5" y="42" width="25" height="15" fill="#1a1a2e" stroke="#333" strokeWidth="0.12" rx="0.5" />

        {/* Tunnel connecting stations */}
        <line x1="-10" y1="3" x2="-5" y2="3" stroke="#2a4a6a" strokeWidth="0.4" />
        <line x1="30" y1="3" x2="35" y2="3" stroke="#2a4a6a" strokeWidth="0.4" strokeDasharray="1 0.5" />
        <line x1="-22.5" y1="10" x2="-22.5" y2="35" stroke="#2a4a6a" strokeWidth="0.3" strokeDasharray="1 0.5" />
        <line x1="17.5" y1="10" x2="17.5" y2="35" stroke="#2a4a6a" strokeWidth="0.3" strokeDasharray="1 0.5" />

        {/* Grid */}
        {[-40, -20, 0, 20, 40].map(x => <line key={'v' + x} x1={x} y1="-10" x2={x} y2="60" stroke="#1e293b" strokeWidth="0.05" />)}
        {[-10, 0, 10, 20, 30, 40, 50, 60].map(y => <line key={'h' + y} x1="-50" y1={y} x2="50" y2={y} stroke="#1e293b" strokeWidth="0.05" />)}

        {/* Mission paths */}
        {missions.map(mission => {
          const isSel = selectedMission === mission.id;
          const points = mission.waypoints.map(w => `${w.x},${w.y}`).join(' ');
          const color = mission.status === 'completed' ? '#6b7280' : mission.status === 'returning' ? '#f59e0b' : '#10b981';
          return (
            <g key={mission.id} onClick={() => onSelectMission(mission.id)} style={{ cursor: 'pointer' }}>
              <polyline
                points={points}
                fill="none"
                stroke={isSel ? '#E3002C' : color}
                strokeWidth={isSel ? 0.5 : 0.3}
                opacity={isSel ? 1 : 0.6}
                strokeDasharray={mission.status === 'scheduled' ? '1 0.5' : 'none'}
              />
              {/* Waypoints */}
              {mission.waypoints.map((wp, i) => (
                <g key={i}>
                  <circle cx={wp.x} cy={wp.y} r={isSel ? 0.8 : 0.5} fill={isSel ? '#E3002C' : color} opacity="0.8" />
                  {isSel && (
                    <text x={wp.x} y={wp.y - 1.5} fill="#fff" fontSize="0.8" textAnchor="middle">
                      {wp.action}
                    </text>
                  )}
                </g>
              ))}
            </g>
          );
        })}

        {/* Robot positions */}
        {robots.map(robot => {
          const color = robot.state === 'active' ? '#10b981' : robot.state === 'docked' ? '#3b82f6' : robot.state === 'returning' ? '#f59e0b' : '#ef4444';
          return (
            <g key={robot.id}>
              <circle cx={robot.pos.x} cy={robot.pos.y} r="1.2" fill={color} stroke="#fff" strokeWidth="0.15" />
              <circle cx={robot.pos.x} cy={robot.pos.y} r="1.2" fill="none" stroke={color} strokeWidth="0.15" opacity="0.5">
                <animate attributeName="r" from="1.2" to="4" dur="2s" repeatCount="indefinite" />
                <animate attributeName="opacity" from="0.6" to="0" dur="2s" repeatCount="indefinite" />
              </circle>
              <text x={robot.pos.x} y={robot.pos.y - 2} fill={color} fontSize="1" textAnchor="middle" opacity="0.8">
                {ROBOT_ICONS[robot.type]} {robot.name}
              </text>
            </g>
          );
        })}

        {/* Compass */}
        <g transform="translate(45, -7)">
          <circle cx="0" cy="0" r="2.5" fill="none" stroke="#475569" strokeWidth="0.15" />
          <text x="0" y="-1.5" fill="#64748b" fontSize="1" textAnchor="middle">N</text>
          <line x1="0" y1="-1" x2="0" y2="1" stroke="#64748b" strokeWidth="0.15" />
        </g>
      </svg>

      {/* Overlays */}
      <div className="absolute left-3 top-3 rounded-lg bg-black/60 px-3 py-2 text-xs text-white backdrop-blur">
        <div className="font-semibold">Mission Map</div>
        <div className="mt-1 text-white/60">Drag to pan · Scroll to zoom · Click path to select</div>
      </div>
      <button onClick={() => setViewBox({ x: -50, y: -10, w: 100, h: 70 })} className="absolute right-3 top-3 rounded bg-black/60 px-2 py-1 text-xs text-white backdrop-blur hover:bg-black/80">⟲ Reset</button>
      <div className="absolute bottom-3 left-3 flex flex-col gap-1 rounded-lg bg-black/60 px-3 py-2 text-xs text-white backdrop-blur">
        <div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-green-500" /> Active Robot</div>
        <div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-amber-500" /> Returning</div>
        <div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-red-500" /> Offline</div>
        <div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-blue-500" /> Docked</div>
      </div>
    </div>
  );
}

export default function Missions() {
  const robots = getRobots();
  const missions = getMissions();
  const { db } = useConsoleState();
  const [selectedMission, setSelectedMission] = useState(null);
  const [selectedRobot, setSelectedRobot] = useState(null);
  const [showRerun, setShowRerun] = useState(false);

  const selMission = selectedMission ? getMissionById(selectedMission) : null;
  const selRobot = selectedMission ? robots.find(r => r.id === selMission?.robotId) : null;
  const rerunAddr = db?.rerun?.viewer_addr || '127.0.0.1:9876';
  const rerunLive = db?.rerun?.listening;

  // Plot mission path in Rerun when a mission is selected and Rerun view is enabled
  useEffect(() => {
    if (showRerun && rerunLive && selMission?.waypoints?.length) {
      const robot = robots.find(r => r.id === selMission.robotId);
      const robotY = robot?.pos?.y ?? 20; // use robot's Y (station length) as ground level
      const waypoints = selMission.waypoints.map((wp, i) => ({
        x: wp.x,
        y: robotY,   // ground at robot's Y position in station
        z: wp.y,     // 2D map y → 3D z (depth)
        label: `WP${i + 1}: ${wp.action}`,
      }));
      // Add robot starting position as first waypoint
      if (robot) {
        waypoints.unshift({ x: robot.pos.x, y: robot.pos.y, z: robot.pos.z, label: robot.name });
      }
      fetch(apiURL('/api/rerun/plot-path'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ waypoints, label: selMission.name }),
      }).catch(() => {});
    }
  }, [showRerun, rerunLive, selectedMission]);

  const handleSelectMission = (missionId) => {
    setSelectedMission(missionId);
    // Auto-show Rerun when a mission is selected
    if (rerunLive) setShowRerun(true);
  };

  return (
    <div className="mx-auto max-w-7xl px-6 py-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Missions & Fleet</h1>
        <p className="mt-1 text-sm text-gray-500">Real-time robot tracking and inspection mission management</p>
      </div>

      {/* Robot Fleet Directory */}
      <div className="mb-6 rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
        <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-700">Robot Fleet</h2>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
          {robots.map(robot => {
            const batteryColor = robot.battery > 50 ? 'text-green-600' : robot.battery > 20 ? 'text-amber-600' : 'text-red-600';
            const batteryBg = robot.battery > 50 ? 'bg-green-500' : robot.battery > 20 ? 'bg-amber-500' : 'bg-red-500';
            return (
              <Link
                key={robot.id}
                to={'/robot/' + robot.id}
                className={'block rounded-lg border p-4 transition-colors ' + (selectedRobot === robot.id ? 'border-[#E3002C] ring-1 ring-[#E3002C]/20' : 'border-gray-100 hover:border-gray-300')}
                onClick={() => {
                  setSelectedRobot(robot.id);
                  const m = missions.find(mi => mi.robotId === robot.id && mi.status !== 'completed');
                  if (m) setSelectedMission(m.id);
                }}
              >
                <div className="flex items-center gap-2">
                  <span className="text-2xl">{ROBOT_ICONS[robot.type] || '🤖'}</span>
                  <div className="flex-1 min-w-0">
                    <div className="truncate text-sm font-semibold text-gray-900">{robot.name}</div>
                    <div className="truncate text-xs text-gray-500">{robot.id}</div>
                  </div>
                  <span className={'rounded px-1.5 py-0.5 text-[9px] font-medium ' + (STATE_COLORS[robot.state] || 'bg-gray-50 text-gray-500')}>
                    {robot.state}
                  </span>
                </div>
                <div className="mt-3 space-y-1.5 text-xs">
                  <div className="flex justify-between"><span className="text-gray-500">Station</span><span className="font-medium text-gray-700">{robot.station}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Battery</span><span className={'font-bold ' + batteryColor}>{robot.battery}%</span></div>
                  <div className="h-1.5 w-full rounded-full bg-gray-100">
                    <div className={'h-1.5 rounded-full ' + batteryBg} style={{ width: robot.battery + '%' }} />
                  </div>
                  <div className="flex justify-between"><span className="text-gray-500">Signal</span><span className="font-mono text-gray-600">{robot.signal != null ? robot.signal + ' dBm' : '—'}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Temp</span><span className="font-mono text-gray-600">{robot.temp}°C</span></div>
                </div>
              </Link>
            );
          })}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-4">
        {/* Left: Mission list */}
        <div className="space-y-3">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-700">Active Missions</h2>
          {missions.map(mission => {
            const robot = robots.find(r => r.id === mission.robotId);
            const isSel = selectedMission === mission.id;
            return (
              <button
                key={mission.id}
                onClick={() => { handleSelectMission(mission.id); setSelectedRobot(mission.robotId); }}
                className={'block w-full rounded-lg border p-3 text-left transition-colors ' + (isSel ? 'border-[#E3002C] ring-1 ring-[#E3002C]/20 bg-red-50/30' : 'border-gray-100 hover:border-gray-300 hover:bg-gray-50')}
              >
                <div className="flex items-center gap-2">
                  <span className="text-lg">{ROBOT_ICONS[robot?.type] || '🤖'}</span>
                  <div className="flex-1 min-w-0">
                    <div className="truncate text-sm font-semibold text-gray-900">{mission.name}</div>
                    <div className="truncate text-xs text-gray-500">{mission.station} · {mission.type}</div>
                  </div>
                  <span className={'rounded px-1.5 py-0.5 text-[9px] font-medium ' + (MISSION_STATUS_COLORS[mission.status] || 'bg-gray-50 text-gray-500')}>
                    {mission.status}
                  </span>
                </div>
                <div className="mt-2 flex items-center gap-2 text-xs">
                  <div className="flex-1">
                    <div className="flex items-center justify-between">
                      <span className="text-gray-500">Progress</span>
                      <span className="font-bold text-gray-700">{mission.progress}%</span>
                    </div>
                    <div className="mt-1 h-1.5 w-full rounded-full bg-gray-100">
                      <div className="h-1.5 rounded-full bg-[#E3002C]" style={{ width: mission.progress + '%' }} />
                    </div>
                  </div>
                </div>
                <div className="mt-2 flex items-center justify-between text-[10px] text-gray-500">
                  <span>⏰ {mission.window}</span>
                  {mission.anomalies > 0 && <span className="font-medium text-amber-600">{mission.anomalies} anomalies</span>}
                </div>
              </button>
            );
          })}
        </div>

        {/* Right: Map + mission detail */}
        <div className="lg:col-span-3 space-y-4">
          <MissionsMap missions={missions} robots={robots} selectedMission={selectedMission} onSelectMission={handleSelectMission} />

          {/* Embedded Rerun 3D viewer showing the inspection path */}
          {selMission && rerunLive && (
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <div className="mb-3 flex items-center justify-between">
                <h3 className="text-sm font-semibold uppercase tracking-wider text-gray-700">3D Inspection Path — {selMission.name}</h3>
                <div className="flex items-center gap-3">
                  <span className="flex items-center gap-1.5 text-xs text-green-600">
                    <span className="h-2 w-2 rounded-full bg-green-500 animate-pulse" /> Rerun Live
                  </span>
                  <button
                    onClick={() => setShowRerun(!showRerun)}
                    className={'rounded-lg px-3 py-1 text-xs font-medium transition-colors ' + (showRerun ? 'bg-gray-900 text-white' : 'border border-gray-200 bg-white text-gray-600 hover:bg-gray-50')}
                  >
                    {showRerun ? 'Hide 3D' : 'Show 3D'}
                  </button>
                </div>
              </div>
              {showRerun ? (
                <RerunViewer addr={rerunAddr} height="400px" className="overflow-hidden rounded-lg border border-gray-200" />
              ) : (
                <div className="flex h-[100px] items-center justify-center rounded-lg bg-gray-50 text-sm text-gray-400">
                  Click "Show 3D" to view the inspection path with waypoints in the Rerun 3D viewer
                </div>
              )}
            </div>
          )}

          {selMission && (
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <div className="mb-4 flex items-center justify-between">
                <div>
                  <h3 className="text-lg font-bold text-gray-900">{selMission.name}</h3>
                  <div className="mt-1 text-sm text-gray-500">{selMission.station} · {selMission.type}</div>
                </div>
                <div className="text-right">
                  <div className="text-sm font-semibold text-gray-900">{selRobot?.name}</div>
                  <div className="text-xs text-gray-500">{selMission.robotId}</div>
                </div>
              </div>

              <div className="mb-4 grid grid-cols-2 gap-4 md:grid-cols-4">
                <div className="rounded-lg bg-gray-50 p-3">
                  <div className="text-xs text-gray-500">Progress</div>
                  <div className="mt-1 text-lg font-bold text-[#E3002C]">{selMission.progress}%</div>
                  <div className="mt-1 h-1.5 rounded-full bg-gray-200">
                    <div className="h-1.5 rounded-full bg-[#E3002C]" style={{ width: selMission.progress + '%' }} />
                  </div>
                </div>
                <div className="rounded-lg bg-gray-50 p-3">
                  <div className="text-xs text-gray-500">Time Window</div>
                  <div className="mt-1 text-sm font-bold text-gray-700">{selMission.window}</div>
                  <div className="mt-0.5 text-xs text-gray-500">ETA: {selMission.eta}</div>
                </div>
                <div className="rounded-lg bg-gray-50 p-3">
                  <div className="text-xs text-gray-500">Anomalies</div>
                  <div className={'mt-1 text-lg font-bold ' + (selMission.anomalies > 0 ? 'text-amber-600' : 'text-green-600')}>
                    {selMission.anomalies}
                  </div>
                </div>
                <div className="rounded-lg bg-gray-50 p-3">
                  <div className="text-xs text-gray-500">Robot Battery</div>
                  <div className={'mt-1 text-lg font-bold ' + (selRobot?.battery > 50 ? 'text-green-600' : selRobot?.battery > 20 ? 'text-amber-600' : 'text-red-600')}>
                    {selRobot?.battery}%
                  </div>
                </div>
              </div>

              {/* Waypoints */}
              <div>
                <h4 className="mb-3 text-xs font-semibold uppercase tracking-wider text-gray-700">Patrol Waypoints</h4>
                <div className="relative">
                  <div className="absolute left-4 top-0 bottom-0 w-0.5 bg-gray-200" />
                  <div className="space-y-3 pl-10">
                    {selMission.waypoints.map((wp, i) => {
                      const progressPerWp = 100 / selMission.waypoints.length;
                      const isDone = i * progressPerWp < selMission.progress;
                      const isCurrent = Math.abs(i * progressPerWp - selMission.progress) < progressPerWp;
                      return (
                        <div key={i} className="relative">
                          <div className={'absolute -left-7 top-1 h-3 w-3 rounded-full border-2 ' + (isDone ? 'border-green-500 bg-green-500' : isCurrent ? 'border-[#E3002C] bg-white' : 'border-gray-300 bg-white')} />
                          <div className={'rounded-lg p-2.5 ' + (isDone ? 'bg-green-50' : isCurrent ? 'bg-red-50' : 'bg-gray-50')}>
                            <div className="flex items-center justify-between">
                              <span className="text-sm font-medium text-gray-800">WP{i + 1}: {wp.action}</span>
                              {isDone && <span className="text-xs text-green-600">✓ Done</span>}
                              {isCurrent && <span className="text-xs text-[#E3002C]">● In Progress</span>}
                            </div>
                            <div className="mt-0.5 font-mono text-[10px] text-gray-400">
                              [{wp.x}, {wp.y}]
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </div>
            </div>
          )}

          {!selMission && (
            <div className="rounded-xl border border-gray-200 bg-white p-8 text-center shadow-sm">
              <div className="text-3xl mb-2">🗺️</div>
              <p className="text-sm text-gray-400">Select a mission to view inspection path details</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}