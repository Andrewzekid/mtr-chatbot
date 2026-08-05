import { useState, useRef, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import { apiURL } from '../api/client';
import FrameStrip from '../components/FrameStrip';
import RerunViewer from '../components/RerunViewer';
import { useConsoleState } from '../state/ConsoleState';

const CAT_COLORS = {
  'Lights': '#fbbf24', 'Advertisement Board': '#3b82f6', 'Ticket Gate': '#a855f7',
  'Map': '#22c55e', 'TV': '#6366f1', 'Exit Sign': '#ef4444',
};

function ObjectMiniMap({ obj, anomalies }) {
  const svgRef = useRef(null);
  const [viewBox, setViewBox] = useState({ x: -50, y: -5, w: 100, h: 60 });
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

  const objX = obj?.centroid_x ?? 0;
  const objY = obj?.centroid_z ?? 0;
  const objColor = CAT_COLORS[obj?.category] || '#3b82f6';

  return (
    <div className="relative h-48 w-full overflow-hidden rounded-lg bg-gray-900" style={{ cursor: isDragging ? 'grabbing' : 'grab' }}>
      <svg ref={svgRef} className="absolute inset-0 h-full w-full" viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
        onMouseDown={onMouseDown} onMouseMove={onMouseMove} onMouseUp={onMouseUp} onMouseLeave={onMouseUp} onWheel={handleWheel}>
        <rect x="-100" y="-50" width="200" height="120" fill="#0f172a" />
        {/* Platform */}
        <rect x="-45" y="0" width="90" height="6" fill="#1e3a5f" stroke="#3b5998" strokeWidth="0.2" />
        {/* Concourse */}
        <rect x="-45" y="12" width="90" height="25" fill="#1a1a2e" stroke="#333" strokeWidth="0.15" />
        {/* Grid */}
        {[-40, -20, 0, 20, 40].map(x => <line key={`v${x}`} x1={x} y1="-5" x2={x} y2="55" stroke="#1e293b" strokeWidth="0.05" />)}
        {[-5, 10, 25, 40, 55].map(y => <line key={`h${y}`} x1="-50" y1={y} x2="50" y2={y} stroke="#1e293b" strokeWidth="0.05" />)}

        {/* Object position */}
        <circle cx={objX} cy={objY} r="1.5" fill={objColor} stroke="#fff" strokeWidth="0.2" />
        <circle cx={objX} cy={objY} r="1.5" fill="none" stroke={objColor} strokeWidth="0.15" opacity="0.5">
          <animate attributeName="r" from="1.5" to="5" dur="2s" repeatCount="indefinite" />
          <animate attributeName="opacity" from="0.6" to="0" dur="2s" repeatCount="indefinite" />
        </circle>
        <text x={objX} y={objY - 2.5} fill="#fff" fontSize="1.2" textAnchor="middle">{obj?.category}</text>

        {/* Nearby anomalies */}
        {(anomalies || []).map((a, i) => {
          const ax = a.cam_x ?? 0, ay = a.cam_z ?? 0;
          return (
            <g key={i}>
              <circle cx={ax} cy={ay} r="0.8" fill="#ef4444" opacity="0.7">
                <title>{a.object}: {a.type}</title>
              </circle>
            </g>
          );
        })}
      </svg>
      <div className="absolute left-2 top-2 rounded bg-black/60 px-2 py-1 text-[10px] text-white backdrop-blur">
        Drag to pan · Scroll to zoom
      </div>
      <button onClick={() => setViewBox({ x: -50, y: -5, w: 100, h: 60 })} className="absolute right-2 top-2 rounded bg-black/60 px-2 py-1 text-[10px] text-white backdrop-blur hover:bg-black/80">Reset</button>
    </div>
  );
}

export default function ObjectDetail() {
  const { id } = useParams();
  const { db, addHighlightLog } = useConsoleState();
  const { data: obj, loading } = useApi(`/api/objects/${id}`);
  const { data: frames } = useApi(`/api/objects/${id}/frames`);
  const { data: movement } = useApi(`/api/objects/${id}/movement`);
  const { data: nearby } = useApi(`/api/objects/${id}/nearby?radius_m=3`);
  const [show3D, setShow3D] = useState(false);

  const rerunAddr = db?.rerun?.viewer_addr || '127.0.0.1:9876';
  const rerunLive = db?.rerun?.listening;

  const handleShow3D = async () => {
    setShow3D(true);
    try {
      await fetch(apiURL('/api/rerun/highlight'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ object_ids: [parseInt(id)], categories: [], focus: true }),
      });
      addHighlightLog({ status: 'ok', object: id });
    } catch (e) {
      addHighlightLog({ status: 'error' });
    }
  };
  const { data: anomalies } = useApi('/api/anomalies?limit=all');

  if (loading) return <div className="flex items-center justify-center py-20 text-gray-400">Loading...</div>;
  if (!obj) return <div className="py-20 text-center text-gray-400">Object #{id} not found</div>;

  const objName = `${obj.category} #${obj.id}`;
  const coords = obj.centroid_x != null
    ? { x: obj.centroid_x.toFixed(2), y: obj.centroid_y.toFixed(2), z: obj.centroid_z.toFixed(2) }
    : null;

  // Find nearby anomalies (within radius of this object)
  const nearbyAnomalies = (anomalies || []).filter(a => {
    if (a.cam_x == null || obj.centroid_x == null) return false;
    const dx = a.cam_x - obj.centroid_x;
    const dy = a.cam_y - obj.centroid_y;
    const dz = a.cam_z - obj.centroid_z;
    const dist = Math.sqrt(dx*dx + dy*dy + dz*dz);
    return dist < 5.0;
  });

  // Build timeline from movement data
  const timeline = (movement || []).map((m, i) => ({
    time: m.timestamp_ns_iso?.slice(11, 19) || `T+${i}`,
    count: 1,
    cumulative: i + 1,
    coords: m.centroid_x != null ? `[${m.centroid_x?.toFixed(1)}, ${m.centroid_y?.toFixed(1)}, ${m.centroid_z?.toFixed(1)}]` : null,
  }));

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      {/* Header */}
      <div className="mb-8 flex items-start justify-between">
        <div>
          <Link to="/overview" className="mb-2 inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700">← Overview</Link>
          <h1 className="mt-1 text-2xl font-bold text-gray-900">{objName}</h1>
          <div className="mt-2 flex items-center gap-3">
            <span className="inline-flex items-center rounded-full px-3 py-1 text-sm font-medium" style={{ background: (CAT_COLORS[obj.category] || '#3b82f6') + '20', color: CAT_COLORS[obj.category] || '#3b82f6' }}>
              {obj.category}
            </span>
            <span className="text-sm text-gray-500">{obj.detection_count} detection{obj.detection_count !== 1 ? 's' : ''}</span>
            {obj.is_gt ? (
              <span className="inline-flex items-center rounded-full bg-green-50 px-2.5 py-0.5 text-xs font-medium text-green-700 ring-1 ring-inset ring-green-600/20">Ground Truth</span>
            ) : null}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={handleShow3D}
            className="inline-flex items-center gap-1.5 rounded-lg bg-gray-900 px-3.5 py-2 text-sm font-semibold text-white shadow-sm transition-all hover:bg-gray-700 active:scale-95"
          >
            🎮 Show in 3D
          </button>
          {show3D && (
            <button
              onClick={() => setShow3D(false)}
              className="rounded-lg border border-gray-200 bg-white px-3.5 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
            >
              Hide 3D
            </button>
          )}
        </div>
      </div>

      {/* Embedded 3D viewer */}
      {show3D && rerunLive && (
        <div className="mb-8">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-700">3D Viewer — {objName}</h2>
            <span className="flex items-center gap-1.5 text-xs text-green-600">
              <span className="h-2 w-2 rounded-full bg-green-500 animate-pulse" /> Connected to Rerun
            </span>
          </div>
          <RerunViewer addr={rerunAddr} height="500px" className="overflow-hidden rounded-xl border border-gray-200 shadow-sm" />
        </div>
      )}
      {show3D && !rerunLive && (
        <div className="mb-8 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-700">
          3D viewer is offline. Start the Rerun viewer to see this object in 3D.
        </div>
      )}

      <div className="grid grid-cols-1 gap-8 lg:grid-cols-3">
        {/* Left column */}
        <div className="space-y-6 lg:col-span-2">
          {/* Frames */}
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-900">Captured Frames</h2>
            <FrameStrip urls={frames || []} title={`${objName} frames`} />
          </div>

          {/* Timeline with timestamps and counts */}
          {timeline.length > 0 && (
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-900">Detection Timeline</h2>
              <div className="relative">
                <div className="absolute left-4 top-0 bottom-0 w-0.5 bg-gray-200" />
                <div className="space-y-3 pl-10">
                  {timeline.map((t, i) => (
                    <div key={i} className="relative">
                      <div className="absolute -left-7 top-1 h-3 w-3 rounded-full border-2 border-white bg-[#E3002C] shadow" />
                      <div className="rounded-lg bg-gray-50 p-3">
                        <div className="flex items-center justify-between">
                          <span className="font-mono text-xs text-gray-700">{t.time}</span>
                          <span className="rounded-full bg-[#E3002C]/10 px-2 py-0.5 text-xs font-semibold text-[#E3002C]">
                            #{t.cumulative}
                          </span>
                        </div>
                        {t.coords && (
                          <div className="mt-1 font-mono text-xs text-gray-500">{t.coords}</div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* Nearby anomalies */}
          {nearbyAnomalies.length > 0 && (
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-900">Nearby Anomalies</h2>
              <div className="space-y-2">
                {nearbyAnomalies.map(a => {
                  const dx = a.cam_x - obj.centroid_x;
                  const dy = a.cam_y - obj.centroid_y;
                  const dz = a.cam_z - obj.centroid_z;
                  const dist = Math.sqrt(dx*dx + dy*dy + dz*dz);
                  return (
                    <Link
                      key={a.id}
                      to={`/defects?focus=${a.id}`}
                      className="flex items-center gap-3 rounded-lg border-l-4 border-amber-400 bg-amber-50/50 p-3 transition-colors hover:bg-amber-50"
                    >
                      <div className="flex-1">
                        <div className="text-sm font-medium text-gray-900">{a.object || 'Unknown'}</div>
                        <div className="mt-0.5 text-xs text-gray-500">{a.type}</div>
                      </div>
                      <span className="rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-semibold text-amber-700">
                        {dist.toFixed(2)}m
                      </span>
                    </Link>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        {/* Right column */}
        <div className="space-y-6">
          {/* 3D Position map */}
          {coords && (
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-900">3D Position Map</h2>
              <ObjectMiniMap obj={obj} anomalies={anomalies} />
              <div className="mt-4 grid grid-cols-3 gap-2">
                <div className="rounded-lg bg-red-50 p-3 text-center">
                  <div className="text-xs font-medium text-red-600">X</div>
                  <div className="mt-1 font-mono text-lg font-bold text-red-700">{coords.x}</div>
                </div>
                <div className="rounded-lg bg-green-50 p-3 text-center">
                  <div className="text-xs font-medium text-green-600">Y</div>
                  <div className="mt-1 font-mono text-lg font-bold text-green-700">{coords.y}</div>
                </div>
                <div className="rounded-lg bg-blue-50 p-3 text-center">
                  <div className="text-xs font-medium text-blue-600">Z</div>
                  <div className="mt-1 font-mono text-lg font-bold text-blue-700">{coords.z}</div>
                </div>
              </div>
              <div className="mt-3 rounded-lg bg-gray-50 p-3 text-xs text-gray-500">
                <div>Bounds: [{obj.min_x?.toFixed(1)}, {obj.min_y?.toFixed(1)}, {obj.min_z?.toFixed(1)}]</div>
                <div>→ [{obj.max_x?.toFixed(1)}, {obj.max_y?.toFixed(1)}, {obj.max_z?.toFixed(1)}]</div>
              </div>
            </div>
          )}

          {/* Timeline summary */}
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-900">Timeline</h2>
            <div className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-500">First seen</span>
                <span className="font-mono text-gray-900">{obj.first_seen_ns_iso?.slice(11, 19) || '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Last seen</span>
                <span className="font-mono text-gray-900">{obj.last_seen_ns_iso?.slice(11, 19) || '—'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Duration</span>
                <span className="font-mono text-gray-900">
                  {obj.first_seen_ns && obj.last_seen_ns
                    ? `${((obj.last_seen_ns - obj.first_seen_ns) / 1e9).toFixed(1)}s`
                    : '—'}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Observations</span>
                <span className="font-mono text-gray-900">{obj.detection_count}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Inspection</span>
                <span className="font-mono text-gray-900">#{obj.inspection_id}</span>
              </div>
            </div>
          </div>

          {/* Nearby objects */}
          {nearby?.length > 0 && (
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-900">Nearby Objects</h2>
              <div className="space-y-2">
                {nearby.map(n => (
                  <Link
                    key={n.id}
                    to={`/objects/${n.id}`}
                    className="flex items-center justify-between rounded-lg border border-gray-100 p-3 transition-colors hover:border-[#E3002C]/30 hover:bg-red-50/50"
                  >
                    <div>
                      <span className="font-medium text-gray-900">{n.category}</span>
                      <span className="ml-1.5 font-mono text-xs text-gray-400">#{n.id}</span>
                    </div>
                    <span className="rounded-full bg-[#E3002C]/10 px-2.5 py-0.5 text-xs font-semibold text-[#E3002C]">
                      {n.distance_m?.toFixed(2)}m
                    </span>
                  </Link>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}