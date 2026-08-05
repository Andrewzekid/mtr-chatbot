import { useEffect, useRef, useState } from 'react';
import { WebViewer } from '@rerun-io/web-viewer';

export default function RerunViewer({ addr, className = '', height = '500px' }) {
  const containerRef = useRef(null);
  const viewerRef = useRef(null);
  const [status, setStatus] = useState('loading');
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;

    async function init() {
      if (!containerRef.current) return;
      try {
        setStatus('loading');
        const viewer = new WebViewer();
        viewerRef.current = viewer;

        const connectUrl = `rerun+http://${addr}/proxy`;
        await viewer.start(connectUrl, containerRef.current, {
          width: '100%',
          height: height,
          hide_welcome_screen: true,
        });

        if (!cancelled) {
          setStatus('connected');
        }
      } catch (e) {
        if (!cancelled) {
          setStatus('error');
          setError(e.message || String(e));
        }
      }
    }

    init();

    return () => {
      cancelled = true;
      if (viewerRef.current) {
        try { viewerRef.current.stop(); } catch (e) {}
        viewerRef.current = null;
      }
    };
  }, [addr, height]);

  return (
    <div className={className} style={{ position: 'relative' }}>
      <div
        ref={containerRef}
        style={{ width: '100%', height, background: '#1a1a2e', borderRadius: '8px', overflow: 'hidden' }}
      />
      {status === 'loading' && (
        <div className="absolute inset-0 flex items-center justify-center bg-gray-900 text-sm text-gray-400">
          <div className="text-center">
            <div className="mb-2 animate-pulse">Loading 3D Viewer...</div>
            <div className="text-xs text-gray-600">Connecting to {addr}</div>
          </div>
        </div>
      )}
      {status === 'error' && (
        <div className="absolute inset-0 flex items-center justify-center bg-gray-900 text-center text-sm text-red-400">
          <div>
            <div className="mb-2 text-3xl">⚠️</div>
            <div>Failed to connect to Rerun viewer</div>
            <div className="mt-1 text-xs text-gray-500">{error}</div>
            <div className="mt-2 text-xs text-gray-600">
              Make sure the Rerun viewer is running at <span className="font-mono">{addr}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}