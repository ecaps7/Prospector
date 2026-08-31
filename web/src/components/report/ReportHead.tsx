type Props = {
  title: string;
};

export function ReportHead({ title }: Props) {
  return (
    <div className="report-head">
      <div className="report-title-row">
        <h1>{title}</h1>
      </div>
    </div>
  );
}
