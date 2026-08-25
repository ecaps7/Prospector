import type { ReportSource } from "../../api/types";

type Props = {
  sources: ReportSource[];
  onOpenSource: (source: ReportSource) => void;
};

export function SourceList({ sources, onOpenSource }: Props) {
  return (
    <div className="sources-list">
      {sources.map((source) => (
        <button
          key={source.citation_number}
          className="source-item"
          type="button"
          onClick={() => onOpenSource(source)}
        >
          <span className="source-num">{source.citation_number}</span>
          <div className="source-main">
            <div className="source-title">
              {source.title || source.source_uri}
              {source.author ? ` · ${source.author}` : ""}
            </div>
            <span className="source-uri">{source.source_uri}</span>
          </div>
        </button>
      ))}
    </div>
  );
}
