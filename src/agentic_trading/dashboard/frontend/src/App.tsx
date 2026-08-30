import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, BookSummary } from "./api";
import Overview from "./pages/Overview";
import Positions from "./pages/Positions";
import Trades from "./pages/Trades";
import Decisions from "./pages/Decisions";
import Logic from "./pages/Logic";
import Roster from "./pages/Roster";
import Compare from "./pages/Compare";

const PAGES = [
  { path: "overview", label: "总览" },
  { path: "positions", label: "持仓" },
  { path: "trades", label: "交易记录" },
  { path: "decisions", label: "决策链" },
  { path: "logic", label: "策略逻辑" },
];

export default function App() {
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.books().then((d) => setBooks(d.books)).catch((e) => setError(String(e)));
  }, []);

  if (error) return <div className="center error">后端不可达：{error}</div>;
  if (!books.length) return <div className="center">加载中…</div>;

  const first = books[0].id;

  return (
    <div className="shell">
      <header>
        <span className="brand">Agentic Trading</span>
        <nav className="book-tabs">
          {books.map((b) => (
            <NavLink key={b.id} to={`/${b.id}/overview`} className={({ isActive }) => "book-tab"}>
              {b.display_name}
            </NavLink>
          ))}
          <NavLink to="/compare" className="book-tab">双盘对比</NavLink>
        </nav>
      </header>
      <Routes>
        <Route path="/" element={<Navigate to={`/${first}/overview`} replace />} />
        <Route path="/compare" element={<Compare />} />
        {books.map((b) => (
          <Route key={b.id} path={`/${b.id}/*`} element={<BookLayout book={b} />} />
        ))}
      </Routes>
    </div>
  );
}

function BookLayout({ book }: { book: BookSummary }) {
  const isRotation = book.kind === "rs_rotation";
  return (
    <>
      <nav className="page-nav">
        {PAGES.map((p) => (
          <NavLink key={p.path} to={p.path} className={({ isActive }) => (isActive ? "active" : "")}>
            {p.label}
          </NavLink>
        ))}
        {isRotation && (
          <NavLink to="roster" className={({ isActive }) => (isActive ? "active" : "")}>
            轮动名单
          </NavLink>
        )}
      </nav>
      <Routes>
        <Route path="overview" element={<Overview bookId={book.id} />} />
        <Route path="positions" element={<Positions bookId={book.id} />} />
        <Route path="trades" element={<Trades bookId={book.id} />} />
        <Route path="decisions" element={<Decisions bookId={book.id} />} />
        <Route path="logic" element={<Logic bookId={book.id} />} />
        <Route path="roster" element={<Roster bookId={book.id} />} />
        <Route path="*" element={<Navigate to="overview" replace />} />
      </Routes>
    </>
  );
}
