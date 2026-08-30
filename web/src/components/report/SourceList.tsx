import type { ReportSource } from "../../state/reportDoc";

type Props = {
  sources: ReportSource[];
  onOpenSource: (citationNumber: number) => void;
};

export function SourceList({ sources, onOpenSource }: Props) {
  return (
    <div className="sources-list">
      {sources.map((source) => (
        <button
          key={source.number}
          className="source-item"
          type="button"
          onClick={() => onOpenSource(source.number)}
        >
          <span className="source-num">{source.number}</span>
          <div className="source-main">
            <div className="source-title">{source.label}</div>
            <span className="source-uri">{source.uri}</span>
          </div>
        </button>
      ))}
    </div>
  );
}
