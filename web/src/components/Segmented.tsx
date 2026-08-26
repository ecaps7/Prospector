import { useSegThumb } from "./useSegThumb";

type Option<T> = {
  value: T;
  label: string;
};

type Props<T> = {
  options: Option<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
};

export function Segmented<T extends string | number>({ options, value, onChange, label }: Props<T>) {
  const activeIndex = options.findIndex((option) => option.value === value);
  const { rootRef, thumbRef } = useSegThumb(activeIndex, options.length);

  return (
    <div className="seg" role="radiogroup" aria-label={label} ref={rootRef}>
      <span className="seg-thumb" aria-hidden="true" ref={thumbRef} />
      {options.map((option) => (
        <button
          key={String(option.value)}
          type="button"
          role="radio"
          aria-checked={option.value === value}
          data-label={option.label}
          className={option.value === value ? "on" : ""}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
