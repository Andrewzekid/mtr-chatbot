import { useState, useRef, useEffect } from 'react';
import { useApi } from '../hooks/useApi';
import { useConsoleState } from '../state/ConsoleState';
import { apiURL } from '../api/client';
import RerunViewer from '../components/RerunViewer';

const LAYERS = [
  { id: 'objects', label: 'Detected Objects', icon: '📦', color: '#3b82f6' },
  { id: 'anomalies', label: 'Anomaly Markers', icon: '⚠️', color: '#ef4444' },
  { id: 'path', label: 'Inspection Path', icon: '🛤️', color: '#10b981' },
  { id: 'categories', label: 'Category Points', icon: '🏷️', color: '#f59e0b' },
];

const CAT_ICONS = {
  'Lights': '💡', 'Advertisement Board': '📺', 'Ticket Gate': '🚇',
  'Map': '🗺️', 'TV': '📺', 'Exit Sign': '🚷',
};

const CAT_COLORS = {
  'Lights': '#fbbf24', 'Advertisement Board': '#3b82f6', 'Ticket Gate': '#a855f7',
  'Map': '#22c55e', 'TV': '#6366f1', 'Exit Sign': '#ef4444',
};

function InteractiveStationMap({ objects, anomalies, locations, activeLayers, onSelectObject }) {
  const svgRef = useRef(null);
  const [viewBox, setViewBox] = useState({ x: -50, y: -5, w: 100, h: 60 });
  const [isDragging, setIsDragging] = useState(false);
  const dragStart = useRef({ x: 0, y: 0, vbx: 0, vby: 0 });
  const [selectedObj, setSelectedObj] = useState(null);
  const [hoveredItem, setHoveredItem] = useState(null);

  const onMouseDown = (e) => {
    setIsDragging(true);
    const rect = svgRef.current.getBoundingClientRect();
    dragStart.current = {
      x: e.clientX,
      y: e.clientY,
      vbx: viewBox.x,
      vby: viewBox.y,
      rectW: rect.width,
      rectH: rect.height,
    };
  };

  const onMouseMove = (e) => {
    if (!isDragging) return;
    const dx = e.clientX - dragStart.current.x;
    const dy = e.clientY - dragStart.current.y;
    const scaleX = viewBox.w / dragStart.current.rectW;
    const scaleY = viewBox.h / dragStart.current.rectH;
    setViewBox(prev => ({
      ...prev,
      x: dragStart.current.vbx - dx * scaleX,
      y: dragStart.current.vby - dy * scaleY,
    }));
  };

  const onMouseUp = () => setIsDragging(false);

  const handleWheel = (e) => {
    e.preventDefault();
    const scale = e.deltaY > 0 ? 1.1 : 0.9;
    setViewBox(prev => {
      const newW = prev.w * scale;
      const newH = prev.h * scale;
      const cx = prev.x + prev.w / 2;
      const cy = prev.y + prev.h / 2;
      return { x: cx - newW / 2, y: cy - newH / 2, w: newW, h: newH };
    });
  };

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
        <defs>
          <radialGradient id="floor-grad" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#1e293b" />
            <stop offset="100%" stopColor="#0f172a" />
          </radialGradient>
        </defs>
        <rect x="-100" y="-50" width="200" height="120" fill="url(#floor-grad)" />

        {/* Fake station layout */}
        {/* Platform */}
        <rect x="-45" y="0" width="90" height="6" fill="#1e3a5f" stroke="#3b5998" strokeWidth="0.2" rx="0.5" />
        <text x="0" y="3.5" fill="#6080a0" fontSize="1.5" textAnchor="middle">PLATFORM 1</text>

        {/* Track lines */}
        <line x1="-45" y1="-2" x2="45" y2="-2" stroke="#2a4a6a" strokeWidth="0.3" strokeDasharray="2 1" />
        <line x1="-45" y1="8" x2="45" y2="8" stroke="#2a4a6a" strokeWidth="0.3" strokeDasharray="2 1" />

        {/* Concourse area */}
        <rect x="-45" y="12" width="90" height="25" fill="#1a1a2e" stroke="#333" strokeWidth="0.15" rx="1" />
        <text x="0" y="26" fill="#475569" fontSize="1.5" textAnchor="middle">CONCOURSE LEVEL</text>

        {/* Exit signs */}
        <rect x="-44" y="38" width="8" height="4" fill="#1a2a1a" stroke="#22c55e" strokeWidth="0.2" rx="0.3" />
        <text x="-40" y="40.8" fill="#22c55e" fontSize="1" textAnchor="middle">EXIT A</text>
        <rect x="36" y="38" width="8" height="4" fill="#1a2a1a" stroke="#22c55e" strokeWidth="0.2" rx="0.3" />
        <text x="40" y="40.8" fill="#22c55e" fontSize="1" textAnchor="middle">EXIT B</text>

        {/* Ticket gates */}
        <rect x="-10" y="14" width="20" height="3" fill="#2a1a3a" stroke="#a855f7" strokeWidth="0.15" rx="0.3" />
        <text x="0" y="16" fill="#a855f7" fontSize="0.8" textAnchor="middle">TICKET GATES</text>

        {/* Grid */}
        {[-40, -20, 0, 20, 40].map(x => (
          <line key={`v${x}`} x1={x} y1="-5" x2={x} y2="55" stroke="#1e293b" strokeWidth="0.05" />
        ))}
        {[-5, 10, 25, 40, 55].map(y => (
          <line key={`h${y}`} x1="-50" y1={y} x2="50" y2={y} stroke="#1e293b" strokeWidth="0.05" />
        ))}

        {/* Inspection path */}
        {activeLayers.includes('path') && (
          <polyline
            points="-40,20 -30,18 -20,15 -10,10 0,5 10,10 20,15 30,20 40,25"
            fill="none" stroke="#10b981" strokeWidth="0.3" strokeDasharray="1 0.5" opacity="0.6"
          />
        )}

        {/* Category points (objects) */}
        {activeLayers.includes('objects') && (objects || []).map((obj, i) => {
          const x = obj.centroid_x != null ? obj.centroid_x : -45 + (i * 12);
          const y = obj.centroid_z != null ? obj.centroid_z : 15 + (i % 3) * 5;
          const color = CAT_COLORS[obj.category] || '#3b82f6';
          const isSel = selectedObj?.id === obj.id;
          return (
            <g key={obj.id} onClick={() => { setSelectedObj(obj); onSelectObject?.(obj); }}>
              <circle
                cx={x} cy={y} r={isSel ? 1.5 : 0.8}
                fill={color} opacity={isSel ? 1 : 0.7}
                stroke={isSel ? '#fff' : 'none'} strokeWidth="0.15"
              />
              {isSel && (
                <text x={x} y={y - 2} fill="#fff" fontSize="1.2" textAnchor="middle">
                  {obj.category} #{obj.id}
                </text>
              )}
            </g>
          );
        })}

        {/* Anomaly markers */}
        {activeLayers.includes('anomalies') && (locations || []).map((loc, i) => {
          const x = loc.cam_x != null ? loc.cam_x : -20 + i * 5;
          const y = loc.cam_z != null ? loc.cam_z : 20 + i * 3;
          return (
            <g key={`anom-${i}`} onMouseEnter={() => setHoveredItem(loc)} onMouseLeave={() => setHoveredItem(null)}>
              <circle cx={x} cy={y} r="1.2" fill="#ef4444" opacity="0.9">
                <title>{loc.object}: {loc.type}</title>
              </circle>
              <circle cx={x} cy={y} r="1.2" fill="none" stroke="#ef4444" strokeWidth="0.15" opacity="0.5">
                <animate attributeName="r" from="1.2" to="4" dur="2s" repeatCount="indefinite" />
                <animate attributeName="opacity" from="0.6" to="0" dur="2s" repeatCount="indefinite" />
              </circle>
              {hoveredItem === loc && (
                <text x={x} y={y - 2} fill="#fca5a5" fontSize="1" textAnchor="middle">
                  {loc.object}
                </text>
              )}
            </g>
          );
        })}

        {/* Category labels */}
        {activeLayers.includes('categories') && [...new Set((objects || []).map(o => o.category))].map((cat, i) => {
          const objs = (objects || []).filter(o => o.category === cat);
          if (!objs.length) return null;
          const cx = objs.reduce((s, o) => s + (o.centroid_x || 0), 0) / objs.length;
          const cy = objs.reduce((s, o) => s + (o.centroid_z || 0), 0) / objs.length;
          return (
            <text key={cat} x={cx} y={cy - 1.5} fill={CAT_COLORS[cat]} fontSize="1" textAnchor="middle" opacity="0.6">
              {CAT_ICONS[cat]} {cat} ({objs.length})
            </text>
          );
        })}

        {/* Compass */}
        <g transform="translate(45, -3)">
          <circle cx="0" cy="0" r="2.5" fill="none" stroke="#475569" strokeWidth="0.15" />
          <text x="0" y="-1.5" fill="#64748b" fontSize="1" textAnchor="middle">N</text>
          <line x1="0" y1="-1" x2="0" y2="1" stroke="#64748b" strokeWidth="0.15" />
        </g>
      </svg>

      {/* Toolbar overlay */}
      <div className="absolute right-3 top-3 flex flex-col gap-1.5">
        <button onClick={() => setViewBox({ x: -50, y: -5, w: 100, h: 60 })} className="rounded bg-black/60 px-2 py-1 text-xs text-white backdrop-blur hover:bg-black/80" title="Reset view">⟲ Reset</button>
      </div>

      {/* Info overlay */}
      <div className="absolute left-3 top-3 rounded-lg bg-black/60 px-3 py-2 text-xs text-white backdrop-blur">
        <div className="font-semibold">Station Digital Twin</div>
        <div className="mt-1 text-white/60">Drag to pan · Scroll to zoom · Click objects</div>
      </div>

      {/* Legend */}
      <div className="absolute bottom-3 left-3 flex flex-col gap-1 rounded-lg bg-black/60 px-3 py-2 text-xs text-white backdrop-blur">
        <div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-red-500" /> Anomaly</div>
        <div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-blue-500" /> Object</div>
        <div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-green-500" /> Path</div>
      </div>

      {/* Selected object info */}
      {selectedObj && (
        <div className="absolute bottom-3 right-3 max-w-xs rounded-lg bg-black/70 p-3 text-xs text-white backdrop-blur">
          <div className="font-semibold">{selectedObj.category} #{selectedObj.id}</div>
          <div className="mt-1 text-white/60">Detections: {selectedObj.detection_count}</div>
          {selectedObj.centroid_x != null && (
            <div className="font-mono text-[10px] text-white/50">
              [{selectedObj.centroid_x?.toFixed(1)}, {selectedObj.centroid_y?.toFixed(1)}, {selectedObj.centroid_z?.toFixed(1)}]
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function DigitalTwin() {
  const { db, addHighlightLog } = useConsoleState();
  const [activeLayers, setActiveLayers] = useState(['objects', 'anomalies', 'path', 'categories']);
  const [showRerun, setShowRerun] = useState(true);
  const [highlighting, setHighlighting] = useState(false);

  const { data: anomalies } = useApi('/api/anomalies?limit=all');
  const { data: locations } = useApi('/api/anomalies/locations');
  const { data: recentObjects } = useApi('/api/recent-objects?limit=all');
  const { data: categories } = useApi('/api/categories');

  const rerunAddr = db?.rerun?.viewer_addr || '127.0.0.1:9876';
  const rerunLive = db?.rerun?.listening;

  const toggleLayer = (id) => {
    setActiveLayers(prev => prev.includes(id) ? prev.filter(l => l !== id) : [...prev, id]);
  };

  const handleHighlightAll = async () => {
    setHighlighting(true);
    try {
      await fetch(apiURL('/api/rerun/highlight'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ categories: (categories || []).map(c => c.category), focus: false }),
      });
      addHighlightLog({ status: 'ok', action: 'highlight_all' });
    } catch (e) {
      addHighlightLog({ status: 'error' });
    } finally {
      setHighlighting(false);
    }
  };

  const handleClearHighlight = async () => {
    try {
      await fetch(apiURL('/api/rerun/clear'), { method: 'POST' });
      addHighlightLog({ status: 'cleared' });
    } catch (e) {}
  };

  return (
    <div className="mx-auto max-w-7xl px-6 py-8">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Station Digital Twin</h1>
          <p className="mt-1 text-sm text-gray-500">3D spatial visualization of inspection data and anomalies</p>
        </div>
        <div className="flex items-center gap-3">
          <div className={'flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium ' + (rerunLive ? 'bg-green-50 text-green-700' : 'bg-gray-100 text-gray-500')}>
            <span className={'h-2 w-2 rounded-full ' + (rerunLive ? 'bg-green-500 animate-pulse' : 'bg-gray-400')} />
            {rerunLive ? 'Rerun Connected' : 'Rerun Offline'}
          </div>
          <button
            onClick={() => setShowRerun(!showRerun)}
            className={'rounded-lg px-4 py-2 text-sm font-semibold transition-colors ' + (showRerun ? 'bg-gray-900 text-white' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50')}
          >
            {showRerun ? '📊 Map View' : '🎮 Rerun 3D View'}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-4">
        {/* Left: Layer controls */}
        <div className="space-y-4">
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-700">Map Layers</h2>
            <div className="space-y-2">
              {LAYERS.map(layer => (
                <button
                  key={layer.id}
                  onClick={() => toggleLayer(layer.id)}
                  className={'flex w-full items-center gap-3 rounded-lg p-3 text-sm transition-colors ' + (activeLayers.includes(layer.id) ? 'bg-gray-100 font-medium text-gray-900' : 'text-gray-500 hover:bg-gray-50')}
                >
                  <span className="text-lg">{layer.icon}</span>
                  <span className="flex-1 text-left">{layer.label}</span>
                  <span className={'h-4 w-4 rounded border-2 ' + (activeLayers.includes(layer.id) ? 'border-[#E3002C] bg-[#E3002C]' : 'border-gray-300')}>
                    {activeLayers.includes(layer.id) && <svg className="h-full w-full text-white" viewBox="0 0 12 12" fill="none"><path d="M2 6l3 3 5-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>}
                  </span>
                </button>
              ))}
            </div>
          </div>

          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-700">3D Actions</h2>
            <div className="space-y-2">
              <button
                onClick={handleHighlightAll}
                disabled={highlighting || !rerunLive}
                className="w-full rounded-lg bg-gray-900 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-gray-700 disabled:opacity-50"
              >
                {highlighting ? 'Highlighting...' : 'Highlight All Objects'}
              </button>
              <button
                onClick={handleClearHighlight}
                disabled={!rerunLive}
                className="w-full rounded-lg border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:opacity-50"
              >
                Clear Highlights
              </button>
            </div>
            <div className="mt-4 border-t border-gray-100 pt-3 text-xs text-gray-500">
              <div>Viewer: <span className="font-mono">{rerunAddr}</span></div>
              <div className="mt-1">Jobs: {db?.rerun?.job_stats?.jobs_ok ?? 0} ok / {db?.rerun?.job_stats?.jobs_run ?? 0} total</div>
            </div>
          </div>
        </div>

        {/* Center: 3D view */}
        <div className="lg:col-span-3">
          {showRerun && rerunLive ? (
            <RerunViewer addr={rerunAddr} height="600px" className="overflow-hidden rounded-xl border border-gray-200 shadow-sm" />
          ) : showRerun && !rerunLive ? (
            <div>
              <div className="mb-3 rounded-lg bg-amber-50 px-4 py-2 text-sm text-amber-700">
                Rerun viewer is offline — showing map view instead. Start Rerun viewer to see live 3D.
              </div>
              <InteractiveStationMap
                objects={recentObjects || []}
                anomalies={anomalies || []}
                locations={locations || []}
                activeLayers={activeLayers}
              />
            </div>
          ) : (
            <InteractiveStationMap
              objects={recentObjects || []}
              anomalies={anomalies || []}
              locations={locations || []}
              activeLayers={activeLayers}
            />
          )}

          {/* Anomaly locations list */}
          <div className="mt-4 rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <h3 className="mb-3 text-sm font-semibold uppercase tracking-wider text-gray-700">Anomaly Locations in 3D Space</h3>
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-3">
              {(locations || []).map((loc, i) => (
                <div key={i} className="rounded-lg border border-gray-100 p-3 hover:border-red-200 hover:bg-red-50/30">
                  <div className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full bg-red-500" />
                    <span className="text-sm font-medium text-gray-900">{loc.object}</span>
                  </div>
                  <div className="mt-1 text-xs text-gray-500">{loc.type}</div>
                  <div className="mt-1 font-mono text-[10px] text-gray-400">
                    [{loc.cam_x?.toFixed(1)}, {loc.cam_y?.toFixed(1)}, {loc.cam_z?.toFixed(1)}]
                  </div>
                </div>
              ))}
              {!locations?.length && <p className="col-span-full py-4 text-center text-sm text-gray-400">No anomaly locations</p>}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}