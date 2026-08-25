import type { ReportParagraph, ReportSource, StatementKind } from "../../api/types";

const KIND_LABEL: Record<StatementKind, string> = {
  evidence: "引证句",
  derived: "推理句",
  elaboration: "承转句",
  limitation: "局限句",
};

type Props = {
  paragraphs: ReportParagraph[];
  /** statement_id → citation numbers */
  citations: Record<string, number[]>;
  /** statement_ids that did not survive verification */
  failed: Set<string>;
  sourceByNumber: Map<number, ReportSource>;
  onOpenSource: (source: ReportSource) => void;
};

export function StatementParagraphs({
  paragraphs,
  citations,
  failed,
  sourceByNumber,
  onOpenSource,
}: Props) {
  return (
    <>
      {paragraphs.map((paragraph) => (
        <p key={paragraph.paragraph_id}>
          {paragraph.statements.map((statement) => {
            const numbers = citations[statement.statement_id] ?? [];
            const isFailed = failed.has(statement.statement_id);
            return (
              <span key={statement.statement_id} className={isFailed ? "stmt-failed" : undefined}>
                {statement.kind === "limitation" ? <strong>信息局限：</strong> : null}
                {statement.text}
                {isFailed ? (
                  <span className="fail-mark">未通过核对 · {KIND_LABEL[statement.kind]}</span>
                ) : (
                  numbers.map((number) => {
                    const source = sourceByNumber.get(number);
                    return (
                      <button
                        key={`${statement.statement_id}-${number}`}
                        className="cite"
                        type="button"
                        onClick={() => source && onOpenSource(source)}
                      >
                        {number}
                      </button>
                    );
                  })
                )}
              </span>
            );
          })}
        </p>
      ))}
    </>
  );
}
