import { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { useConsoleState } from '../state/ConsoleState';
import ObjectCard from './ObjectCard';

const ALL_CATS = ['Lights', 'Advertisement Board', 'Ticket Gate', 'Map', 'TV', 'Exit Sign'];

export default function ProximityWizard() {
  const { inspectionId } = useConsoleState();
  const [target, setTarget] = useState('Lights');
  const [others, setOthers] = useState(['Ticket Gate']);
  const [radius, setRadius] = useState(2.0);
  const [submitted, setSubmitted] = useState(null);

  const iidParam = inspectionId ? `&inspection_id=${inspectionId}` : '';
  const enabled = submitted !== null;
  const path = enabled
    ? `/api/proximity?target=${encodeURIComponent(submitted.target)}&others=${submitted.others.map(encodeURIComponent).join(',')}&radius_m=${submitted.radius}&with_images=true${iidParam}`
    : null;
  const { data, loading } = useApi(path, { deps: [path], enabled });

  const toggleOther = (cat) => {
    setOthers(prev => prev.includes(cat) ? prev.filter(c => c !== cat) : [...prev, cat]);
  };

  const handleSubmit = () => {
    setSubmitted({ target, others, radius });
  };

  const results = data?.results || [];

  return (
    <div className="proximity-wizard">
      <div className="proximity-controls">
        <div className="pw-row">
          <label>Target:</label>
          <select value={target} onChange={e => setTarget(e.target.value)} className="pw-select">
            {ALL_CATS.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div className="pw-row">
          <label>Nearby categories:</label>
          <div className="pw-chips">
            {ALL_CATS.map(c => (
              <button
                key={c}
                className={`pw-chip${others.includes(c) ? ' active' : ''}`}
                onClick={() => toggleOther(c)}
              >{c}</button>
            ))}
          </div>
        </div>
        <div className="pw-row">
          <label>Radius: {radius}m</label>
          <input type="range" min={0.5} max={10} step={0.5} value={radius}
            onChange={e => setRadius(parseFloat(e.target.value))} className="pw-slider" />
        </div>
        <button className="pw-submit" onClick={handleSubmit}>Find nearby</button>
      </div>

      {enabled && loading && <p className="loading-msg">Searching...</p>}
      {enabled && !loading && (
        <div className="proximity-results">
          <p className="pw-count">{results.length} result(s) found</p>
          {results.map(r => (
            <div key={r.object_id} className="pw-result">
              <div className="pw-result-header">
                <span>Object {r.object_id} at [{r.centroid_x?.toFixed(1)}, {r.centroid_y?.toFixed(1)}, {r.centroid_z?.toFixed(1)}]</span>
              </div>
              <div className="pw-nearby">
                {(r.nearby || []).map(n => (
                  <div key={n.object_id} className="pw-nearby-item">
                    <span className="pw-nearby-cat">{n.category}</span>
                    <span className="pw-nearby-dist">{n.distance_m?.toFixed(2)}m</span>
                    <span className="pw-nearby-id">#{n.object_id}</span>
                    {n.sample_image_path_url && <img src={n.sample_image_path_url} alt="" className="pw-nearby-img" loading="lazy" />}
                  </div>
                ))}
                {!r.nearby?.length && <span className="pw-no-nearby">Nothing nearby</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
