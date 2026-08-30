import { useEffect } from "react";
import type { ClaimEvidence } from "../../api/types";
import type { ReportSource } from "../../state/reportDoc";

type Props = {
  open: boolean;
  source: ReportSource | null;
  /** 这个来源支撑过的每一段存档原文。 */
  evidence: ClaimEvidence[];
  onClose: () => void;
};

/**
 * 点开一个角标看到的东西。原文直接来自审计文档，不必再往摘录接口跑一趟——
 * 后端已经把每条出处对应的那段原文嵌在里面了。
 */
export function EvidenceDrawer({ open, source, evidence, onClose }: Props) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const first = evidence[0]?.excerpt.source ?? null;
  // 同一段原文可能支撑好几处跨度，来源面板里只该出现一次。
  const excerpts = [...new Map(evidence.map((item) => [item.excerpt.excerpt_id, item])).values()];
  const caveats = [...new Set(evidence.flatMap((item) => item.source_caveats))];

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
          <span className="num">{source?.number ?? ""}</span>
          <h4>{first?.title || source?.label || source?.uri || "来源"}</h4>
          <button className="icon-btn" type="button" onClick={onClose} aria-label="关闭证据卡片">
            ✕
          </button>
        </div>
        <div className="evidence-body">
          {caveats.length ? (
            <div className="ev-field">
              <div className="k">核验时的保留意见</div>
              {caveats.map((caveat) => (
                <p key={caveat} className="ev-caveat">
                  {caveat}
                </p>
              ))}
            </div>
          ) : null}
          {excerpts.length === 0 ? (
            <p className="muted">这条来源没有留下可展示的原文摘录。</p>
          ) : null}
          {excerpts.map((item) => (
            <div key={item.excerpt.excerpt_id} className="ev-field">
              <div className="k">原文摘录</div>
              <div className="ev-excerpt">{item.excerpt.text}</div>
            </div>
          ))}
          {source ? (
            <div className="ev-field">
              <div className="k">来源信息</div>
              <div className="ev-meta">
                <div className="row">
                  <span className="k">来源地址</span>
                  <span className="v">
                    <a href={source.uri} target="_blank" rel="noopener noreferrer">
                      {source.uri}
                    </a>
                  </span>
                </div>
                <div className="row">
                  <span className="k">快照版本</span>
                  <span className="v">第 {first?.document_version ?? source.version} 版</span>
                </div>
                {first?.author ? (
                  <div className="row">
                    <span className="k">作者</span>
                    <span className="v">{first.author}</span>
                  </div>
                ) : null}
                {first?.published_at ? (
                  <div className="row">
                    <span className="k">发布时间</span>
                    <span className="v mono">{first.published_at}</span>
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
