import { useEffect } from "react";
import type { ExcerptView, ReportSource } from "../../api/types";

type Props = {
  open: boolean;
  source: ReportSource | null;
  excerpts: ExcerptView[];
  loading: boolean;
  error: string | null;
  onClose: () => void;
};

export function EvidenceDrawer({ open, source, excerpts, loading, error, onClose }: Props) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <>
      <div className={`evidence-backdrop${open ? " open" : ""}`} onClick={onClose} />
      <aside
        className={`evidence-panel${open ? " open" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label="证据摘录"
        aria-hidden={!open}
      >
        <div className="evidence-head">
          <span className="num">{source?.citation_number ?? ""}</span>
          <h4>{source?.title || source?.source_uri || "来源"}</h4>
          <button className="icon-btn" type="button" onClick={onClose} aria-label="关闭证据卡片">
            ✕
          </button>
        </div>
        <div className="evidence-body">
          {loading ? (
            <div className="scope-status" style={{ paddingLeft: 0 }}>
              <span className="spinner" />
              <span>正在读取存档摘录…</span>
            </div>
          ) : null}
          {error ? <p className="form-error">{error}</p> : null}
          {excerpts.map((excerpt) => (
            <div key={excerpt.excerpt_id} className="ev-field">
              <div className="k">存档摘录 · EXCERPT</div>
              <div className="ev-excerpt">{excerpt.text}</div>
            </div>
          ))}
          {source ? (
            <div className="ev-field">
              <div className="k">溯源 · PROVENANCE</div>
              <div className="ev-meta">
                <div className="row">
                  <span className="k">来源地址</span>
                  <span className="v">
                    <a href={source.source_uri} target="_blank" rel="noopener noreferrer">
                      {source.source_uri}
                    </a>
                  </span>
                </div>
                <div className="row">
                  <span className="k">快照版本</span>
                  <span className="v mono">document_version = {source.document_version}</span>
                </div>
                {source.author ? (
                  <div className="row">
                    <span className="k">作者</span>
                    <span className="v">{source.author}</span>
                  </div>
                ) : null}
                {source.published_at ? (
                  <div className="row">
                    <span className="k">发布时间</span>
                    <span className="v mono">{source.published_at}</span>
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
        <div className="evidence-foot">
          证据只来自存档原文：检索到的网页整页保存（内容哈希 + 版本号），引用最终指向「某网页某版本的某一段」。
        </div>
      </aside>
    </>
  );
}
