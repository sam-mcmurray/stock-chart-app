
import { useState } from 'react';
import NavItem from './NavItem';
import { useAuth } from '../auth/authState';
import { FiMenu, FiHome, FiCalendar, FiCpu, FiRss, FiSearch, FiBarChart2, FiSettings, FiUser, FiLogOut } from 'react-icons/fi';
import { hardReplace } from '../lib/hardNav';

const items = [
  { to:'/dashboard', label:'Dashboard', icon:<FiHome /> },
  { to:'/calendar',  label:'Calendar',  icon:<FiCalendar /> },
  { to:'/news',      label:'News feed', icon:<FiRss /> },
  { to:'/news-llm',  label:'News LLM', icon:<FiCpu /> },
  { to:'/scanner',   label:'Scanner',   icon:<FiSearch /> },
  { to:'/research',  label:'Research',  icon:<FiBarChart2 /> },
  { to:'/settings',  label:'Settings',  icon:<FiSettings /> },
  { to:'/profile',   label:'Profile',   icon:<FiUser /> },
];

export default function Sidebar({ collapsed, onToggle, isMobile, drawerOpen, onClose }) {
  const { logout } = useAuth();
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState('');
  const showLabels = (!collapsed && !isMobile) || (isMobile && drawerOpen);

  const handleLogout = async () => {
    setLoggingOut(true);
    setLogoutError('');
    const result = await logout();
    if (!result?.ok) {
      setLogoutError(result?.error || 'Logout failed');
      setLoggingOut(false);
      return;
    }
    if (isMobile && onClose) onClose();
    hardReplace('/');
  };

  return (
    <aside
      className={`sidebar ${collapsed ? 'collapsed':''} ${isMobile ? 'drawer':''} ${drawerOpen ? 'open':''}`}
      aria-hidden={isMobile && !drawerOpen}
      aria-label="Main navigation"
    >
      <div className="brand">
        {/* Desktop burger toggles collapse; on mobile it's in Topbar */}
        <button
          className="burger"
          onClick={isMobile ? onClose : onToggle}
          aria-label={isMobile ? 'Close menu' : (collapsed ? 'Expand menu' : 'Collapse menu')}
        >
          <FiMenu />
        </button>
        {!collapsed && !isMobile && <div className="brand-title">StockApp</div>}
      </div>

      <nav className="nav">
        {items.map(({to,label,icon}) => (
          <NavItem
            key={to}
            to={to}
            icon={icon}
            label={label}
            collapsed={collapsed}
            isMobile={isMobile}
            drawerOpen={drawerOpen}
            onSelect={onClose}
          />
        ))}
        <div className="sidebar-footer">
          {logoutError && showLabels && (
            <div className="sidebar-logout-error" role="alert">{logoutError}</div>
          )}
          <button
            type="button"
            className="sidebar-logout"
            onClick={handleLogout}
            disabled={loggingOut}
            title={!showLabels ? 'Logout' : undefined}
            aria-label="Logout"
          >
            <span className="item-icon"><FiLogOut /></span>
            {showLabels && <span className="item-label">{loggingOut ? 'Signing out...' : 'Logout'}</span>}
          </button>
        </div>
      </nav>
    </aside>
  );
}
