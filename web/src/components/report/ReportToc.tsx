export type TocItem = { id: string; title: string; divider?: boolean };

export function ReportToc({ items, active }: { items: TocItem[]; active: string }) {
  return (
    <aside className="toc">
      <div className="panel-title">目录</div>
      {items.map((item) => (
        <span key={item.id}>
          {item.divider ? <div className="toc-div" /> : null}
          <a
            href={`#${item.id}`}
            className={active === item.id ? "on" : ""}
            onClick={(event) => {
              event.preventDefault();
              document.getElementById(item.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
            }}
          >
            {item.title}
          </a>
        </span>
      ))}
    </aside>
  );
}
