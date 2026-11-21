import { NavLink } from 'react-router-dom';

export default function NavItem({ to, icon, label, collapsed, isMobile=false, drawerOpen=false, onSelect }) {
  const showLabel = (!collapsed && !isMobile) || (isMobile && drawerOpen); // ← show on open drawer
  return (
    <NavLink
      to={to}
      className={({ isActive }) => (isActive ? 'active' : undefined)}
      title={!showLabel ? label : undefined}
      onClick={() => { if (isMobile && onSelect) onSelect(); }}
    >
      <span className="item-icon">{icon}</span>
      {showLabel && <span className="item-label">{label}</span>}
    </NavLink>
  );
}