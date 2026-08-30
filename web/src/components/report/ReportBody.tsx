import { useEffect, useMemo, useRef } from "react";
import { renderReportBody } from "../../state/reportDoc";

type Props = {
  /** 正文 Markdown，不含开头的核对情况和末尾的来源。 */
  body: string;
  onHeadings: (headings: { id: string; title: string; level: number }[]) => void;
  onOpenSource: (citationNumber: number) => void;
};

/**
 * 正文。后端交付的是一份 Markdown 文档，角标已经插在里面，所以这里只做两件事：
 * 渲染它，以及让角标可以点。
 *
 * 用 `dangerouslySetInnerHTML` 是因为渲染结果本来就是 HTML 字符串；这串 HTML
 * 由 `renderReportBody` 产出，那里已经把原始 HTML 丢掉、把链接协议限死了。
 * 角标的点击走事件委托：正文里有几十个角标，逐个挂 React 监听器不值得。
 */
export function ReportBody({ body, onHeadings, onOpenSource }: Props) {
  const rendered = useMemo(() => renderReportBody(body), [body]);
  const seen = useRef<string>("");

  useEffect(() => {
    // 目录来自渲染出来的标题，所以只能在渲染之后往上报；同一份正文只报一次。
    const key = rendered.headings.map((item) => item.id).join("|");
    if (seen.current === key) return;
    seen.current = key;
    onHeadings(rendered.headings);
  }, [rendered, onHeadings]);

  return (
    <article
      className="report-body md"
      onClick={(event) => {
        const target = (event.target as HTMLElement).closest<HTMLElement>("[data-cite]");
        if (!target) return;
        onOpenSource(Number(target.dataset.cite));
      }}
      dangerouslySetInnerHTML={{ __html: rendered.html }}
    />
  );
}
