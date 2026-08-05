export default function EmptyState({ icon = '📭', title = 'Nothing here', subtitle = '', children }) {
  return (
    <div className="py-16 text-center">
      <div className="text-5xl mb-3">{icon}</div>
      <h3 className="text-base font-semibold text-gray-700">{title}</h3>
      {subtitle && <p className="mt-1 text-sm text-gray-400">{subtitle}</p>}
      {children}
    </div>
  );
}
