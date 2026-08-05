import { useState, useEffect } from 'react';
import { useSearchParams, Link } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import { apiURL } from '../api/client';
import { useConsoleState } from '../state/ConsoleState';
import RerunViewer from '../components/RerunViewer';

const SEVERITY = {
  'crack and structure damage': { level: 'Critical', color: 'text-red-700 bg-red-100 ring-red-600/20', border: 'border-red-500' },
  'foreign_object': { level: 'High', color: 'text-orange-700 bg-orange-100 ring-orange-600/20', border: 'border-orange-500' },
  'missing_object': { level: 'High', color: 'text-orange-700 bg-orange-100 ring-orange-600/20', border: 'border-orange-500' },
  'state_change': { level: 'Medium', color: 'text-amber-700 bg-amber-100 ring-amber-600/20', border: 'border-amber-500' },
  'content_change': { level: 'Low', color: 'text-blue-700 bg-blue-100 ring-blue-600/20', border: 'border-blue-500' },
  'stain/graffiti': { level: 'Low', color: 'text-blue-700 bg-blue-100 ring-blue-600/20', border: 'border-blue-500' },
  'relocation': { level: 'Medium', color: 'text-amber-700 bg-amber-100 ring-amber-600/20', border: 'border-amber-500' },
};

function severityFor(type) {
  return SEVERITY[type] || { level: 'Unknown', color: 'text-gray-700 bg-gray-100 ring-gray-600/20', border: 'border-gray-500' };
}

const STATUS = ['Pending Review', 'Verified', 'False Positive', 'Dispatched'];

export default function Defects() {
  const [searchParams, setSearchParams] = useSearchParams();
  const focusId = searchParams.get('focus');
  const { db, addHighlightLog } = useConsoleState();
  const [selectedId, setSelectedId] = useState(null);
  const [statusOverrides, setStatusOverrides] = useState({});
  const [lightbox, setLightbox] = useState(null);
  const [show3D, setShow3D] = useState(false);

  const rerunAddr = db?.rerun?.viewer_addr || '127.0.0.1:9876';
  const rerunLive = db?.rerun?.listening;

  const { data: anomalies, loading } = useApi('/api/anomalies?limit=all');
  const { data: locations } = useApi('/api/anomalies/locations');

  useEffect(() => {
    if (focusId) setSelectedId(parseInt(focusId));
    else if (anomalies?.length) setSelectedId(anomalies[0].id);
  }, [focusId, anomalies]);

  const selected = (anomalies || []).find(a => a.id === selectedId) || anomalies?.[0];
  const selectedLocation = (locations || []).find(l => l.abnormality_id === selected?.id);

  const handleStatusChange = (id, status) => {
    setStatusOverrides(prev => ({ ...prev, [id]: status }));
  };

  const handleHighlight = async () => {
    if (!selected) return;
    setShow3D(true);
    try {
      // Highlight the anomaly location as a coordinate in Rerun
      const coords = [];
      if (selectedLocation) {
        coords.push({
          x: selectedLocation.cam_x,
          y: selectedLocation.cam_y,
          z: selectedLocation.cam_z,
          label: selected.object || selected.type,
          color: [239, 68, 68],
          radius: 0.5,
        });
      }
      await fetch(apiURL('/api/rerun/highlight'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          coordinates: coords,
          label: 'Anomaly #' + selected.id,
          focus: true,
        }),
      });
      addHighlightLog({ status: 'ok', anomaly: selected.id });
    } catch (e) {
      addHighlightLog({ status: 'error' });
    }
  };

  return (
    <div className="mx-auto flex h-[calc(100vh-3.5rem)] max-w-[100vw]">
      {/* Left: Defect list */}
      <div className="w-80 flex-shrink-0 overflow-y-auto border-r border-gray-200 bg-white">
        <div className="sticky top-0 z-10 border-b border-gray-200 bg-white px-4 py-3">
          <h1 className="text-lg font-bold text-gray-900">Defect Center</h1>
          <p className="text-xs text-gray-500">{anomalies?.length || 0} anomalies detected</p>
        </div>
        <div className="p-2">
          {(anomalies || []).map(a => {
            const sev = severityFor(a.type);
            const status = statusOverrides[a.id] || 'Pending Review';
            const isActive = selected?.id === a.id;
            return (
              <button
                key={a.id}
                onClick={() => setSelectedId(a.id)}
                className={'mb-1 w-full rounded-lg border-l-4 p-3 text-left transition-colors ' + sev.border + ' ' + (isActive ? 'bg-gray-100 ring-1 ring-gray-200' : 'hover:bg-gray-50')}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-gray-900">{a.object || 'Unknown'}</div>
                    <div className="mt-0.5 text-xs text-gray-500">{a.type}</div>
                  </div>
                  <span className={'inline-flex flex-shrink-0 items-center rounded-full px-2 py-0.5 text-[10px] font-medium ring-1 ring-inset ' + sev.color}>
                    {sev.level}
                  </span>
                </div>
                <div className="mt-1.5 flex items-center gap-1.5 text-[10px]">
                  <span className={'rounded px-1.5 py-0.5 font-medium ' + (
                    status === 'Verified' ? 'bg-green-50 text-green-700' :
                    status === 'False Positive' ? 'bg-gray-50 text-gray-500' :
                    status === 'Dispatched' ? 'bg-blue-50 text-blue-700' :
                    'bg-amber-50 text-amber-700'
                  )}>{status}</span>
                </div>
              </button>
            );
          })}
          {loading && <p className="py-8 text-center text-sm text-gray-400">Loading...</p>}
          {!loading && !anomalies?.length && <p className="py-8 text-center text-sm text-gray-400">No anomalies found</p>}
        </div>
      </div>

      {/* Right: Detail view */}
      <div className="flex-1 overflow-y-auto bg-gray-50">
        {selected && (
          <div className="mx-auto max-w-5xl p-6">
            {/* Header */}
            <div className="mb-6 flex items-start justify-between">
              <div>
                <Link to="/overview" className="mb-1 inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700">← Overview</Link>
                <h2 className="text-xl font-bold text-gray-900">Anomaly #{selected.id}: {selected.object}</h2>
                <div className="mt-2 flex items-center gap-2">
                  <span className={'inline-flex items-center rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset ' + severityFor(selected.type).color}>
                    {severityFor(selected.type).level}: {selected.type}
                  </span>
                  <span className="text-xs text-gray-500">Inspection #{selected.inspection_id}</span>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <button
                  onClick={handleHighlight}
                  className="inline-flex items-center gap-2 rounded-lg bg-gray-900 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-gray-700"
                >
                  Show in 3D
                </button>
                {show3D && (
                  <button
                    onClick={() => setShow3D(false)}
                    className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
                  >
                    Hide 3D
                  </button>
                )}
              </div>
            </div>

            {/* Embedded 3D viewer */}
            {show3D && rerunLive && (
              <div className="mb-6">
                <div className="mb-3 flex items-center justify-between">
                  <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-700">3D Viewer — Anomaly Location</h2>
                  <span className="flex items-center gap-1.5 text-xs text-green-600">
                    <span className="h-2 w-2 rounded-full bg-green-500 animate-pulse" /> Connected to Rerun
                  </span>
                </div>
                <RerunViewer addr={rerunAddr} height="400px" className="overflow-hidden rounded-xl border border-gray-200 shadow-sm" />
              </div>
            )}
            {show3D && !rerunLive && (
              <div className="mb-6 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-700">
                3D viewer is offline. Start the Rerun viewer to see this anomaly in 3D.
              </div>
            )}

            {/* Side-by-side image comparison */}
            <div className="mb-6 grid grid-cols-2 gap-4">
              <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
                <div className="border-b border-gray-100 bg-green-50 px-4 py-2">
                  <span className="text-xs font-semibold uppercase tracking-wider text-green-700">Ground Truth Baseline</span>
                </div>
                {selected.gt_filename_url && (
                  <img
                    src={apiURL(selected.gt_filename_url)}
                    alt="Ground truth"
                    className="w-full cursor-zoom-in object-contain"
                    style={{ maxHeight: '400px' }}
                    onClick={() => setLightbox(apiURL(selected.gt_filename_url))}
                  />
                )}
                <div className="px-4 py-2 text-xs text-gray-500">{selected.gt_filename}</div>
              </div>
              <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
                <div className="border-b border-gray-100 bg-blue-50 px-4 py-2">
                  <span className="text-xs font-semibold uppercase tracking-wider text-blue-700">Current Inspection</span>
                </div>
                {selected.inspection_filename_url && (
                  <img
                    src={apiURL(selected.inspection_filename_url)}
                    alt="Inspection"
                    className="w-full cursor-zoom-in object-contain"
                    style={{ maxHeight: '400px' }}
                    onClick={() => setLightbox(apiURL(selected.inspection_filename_url))}
                  />
                )}
                <div className="px-4 py-2 text-xs text-gray-500">{selected.inspection_filename}</div>
              </div>
            </div>

            {/* Anomaly details */}
            <div className="mb-6 grid grid-cols-1 gap-4 md:grid-cols-2">
              <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
                <h3 className="mb-3 text-sm font-semibold uppercase tracking-wider text-gray-700">Description</h3>
                <p className="text-sm leading-relaxed text-gray-600">{selected.note || 'No description available.'}</p>
              </div>
              <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
                <h3 className="mb-3 text-sm font-semibold uppercase tracking-wider text-gray-700">Location</h3>
                <div className="space-y-1.5 text-sm">
                  {selected.location && <div className="text-gray-600">{selected.location}</div>}
                  {selectedLocation && (
                    <div className="font-mono text-xs text-gray-500">
                      Position: [{selectedLocation.cam_x?.toFixed(2)}, {selectedLocation.cam_y?.toFixed(2)}, {selectedLocation.cam_z?.toFixed(2)}]
                    </div>
                  )}
                  <div className="font-mono text-xs text-gray-500">
                    Bbox: [{selected.min_x}, {selected.min_y}] → [{selected.max_x}, {selected.max_y}]
                  </div>
                </div>
              </div>
            </div>

            {/* Pair summary */}
            {selected.pair_summary && (
              <div className="mb-6 rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
                <h3 className="mb-3 text-sm font-semibold uppercase tracking-wider text-gray-700">AI Analysis Summary</h3>
                <p className="text-sm leading-relaxed text-gray-600">{selected.pair_summary}</p>
              </div>
            )}

            {/* Verification status */}
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <h3 className="mb-3 text-sm font-semibold uppercase tracking-wider text-gray-700">Verification</h3>
              <div className="flex flex-wrap gap-2">
                {STATUS.map(s => {
                  const current = statusOverrides[selected.id] || 'Pending Review';
                  return (
                    <button
                      key={s}
                      onClick={() => handleStatusChange(selected.id, s)}
                      className={'rounded-lg px-4 py-2 text-sm font-medium transition-colors ' + (
                        current === s
                          ? 'bg-[#E3002C] text-white'
                          : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                      )}
                    >
                      {s}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Lightbox */}
      {lightbox && (
        <div className="fixed inset-0 z-[200] flex cursor-zoom-out items-center justify-center bg-black/90" onClick={() => setLightbox(null)}>
          <img src={lightbox} alt="" className="max-h-[92vh] max-w-[92vw] rounded-lg" />
        </div>
      )}
    </div>
  );
}