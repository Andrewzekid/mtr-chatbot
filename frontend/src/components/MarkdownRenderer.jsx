function renderMarkdown(text) {
  if (!text) return [];
  const parts = [];
  const lines = text.split('\n');
  let key = 0;

  for (const line of lines) {
    // Image markdown: ![caption](url)
    const imgMatch = line.match(/!\[([^\]]*)\]\(([^)]+)\)/);
    if (imgMatch) {
      const caption = imgMatch[1];
      const url = imgMatch[2];
      const fullUrl = url.startsWith('http') || url.startsWith('/') ? url : `/inspection/images/${url}`;
      const isGT = caption.toLowerCase().includes('gt') || caption.toLowerCase().includes('ground truth') || caption.toLowerCase().includes('baseline');
      const isInspection = caption.toLowerCase().includes('inspection') || caption.toLowerCase().includes('current');
      const badgeColor = isGT ? 'bg-green-50 text-green-700' : isInspection ? 'bg-blue-50 text-blue-700' : 'bg-gray-50 text-gray-700';
      const badgeText = isGT ? 'Ground Truth' : isInspection ? 'Inspection' : caption;
      parts.push(
        <div key={key++} className="my-3 overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
          <div className={'flex items-center gap-2 px-4 py-2 text-xs font-semibold uppercase tracking-wider ' + badgeColor}>
            <span>{badgeText}</span>
          </div>
          <img src={fullUrl} alt={caption} className="max-w-full object-contain" style={{ maxHeight: '300px' }} loading="lazy" />
        </div>
      );
      continue;
    }

    // Bold text: **text**
    const boldParts = line.split(/(\*\*[^*]+\*\*)/g);
    const rendered = boldParts.map((p, i) => {
      if (p.startsWith('**') && p.endsWith('**')) {
        return <strong key={i}>{p.slice(2, -2)}</strong>;
      }
      return p;
    });

    if (line.trim()) {
      parts.push(<p key={key++} className="mb-2 leading-relaxed">{rendered}</p>);
    }
  }

  return parts;
}

export { renderMarkdown };