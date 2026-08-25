const EXAMPLE_QUESTIONS = [
  "对比中美欧三地 2024 年以来对生成式 AI 的监管路径，指出它们在训练数据合规上的实质分歧。",
  "固态电池到 2030 年能否在乘用车上规模量产？把整车厂公告、供应链产能和第三方拆解数据放在一起核对。",
  "过去三年远程办公对一线城市写字楼空置率的影响，区分中介口径与政府统计口径的差异。",
  "GLP-1 类减重药的长期停药反弹证据有多强？只采信有对照组的临床研究。",
];

export function ExampleQuestions({ onPick }: { onPick: (question: string) => void }) {
  return (
    <div className="ask-examples">
      <p className="ask-examples-label">或者从这些开始</p>
      <div className="ask-examples-list">
        {EXAMPLE_QUESTIONS.map((example) => (
          <button key={example} className="ask-example" type="button" onClick={() => onPick(example)}>
            {example}
          </button>
        ))}
      </div>
    </div>
  );
}
