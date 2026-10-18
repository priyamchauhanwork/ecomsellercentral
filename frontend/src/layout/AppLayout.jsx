import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { SECTIONS, currentSection, isLinkActive } from "./navigation";

export default function AppLayout() {
  const location = useLocation();
  const section = currentSection(location.pathname);

  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand">
          SellerCentral
        </Link>
        <nav className="topnav" aria-label="Sections">
          {SECTIONS.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.path === "/"}
              className={({ isActive }) => `topnav-link${isActive ? " active" : ""}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </header>

      <div className="body">
        <nav className="sidenav" aria-label={`${section.label} views`}>
          <p className="sidenav-title">{section.label}</p>
          {section.links.map((link) => {
            const active = isLinkActive(link, location);
            return (
              <Link
                key={link.to}
                to={link.to}
                className={`sidenav-link${active ? " active" : ""}`}
                aria-current={active ? "page" : undefined}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
