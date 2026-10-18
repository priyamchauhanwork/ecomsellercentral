import { Link } from "react-router-dom";

// Pages whose tickets aren't built yet.
export default function Placeholder({ title, ticket, backTo }) {
  return (
    <section>
      <h1 className="page-title">{title}</h1>
      <p className="muted">This page arrives with {ticket}.</p>
      {backTo && <Link to={backTo.to}>← {backTo.label}</Link>}
    </section>
  );
}
