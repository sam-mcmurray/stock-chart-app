import { FiMenu } from 'react-icons/fi';

export default function Topbar({ onMenu, title = 'StockApp' }) {
  return (
    <header className="topbar">
      <button
        className="burger topbar-menu-button"
        aria-label="Open menu"
        onClick={onMenu}
      >
        <FiMenu />
      </button>
      <div className="title">{title}</div>
    </header>
  );
}
