import type { ReactNode } from "react";

type SwitchProps = {
  id: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  icon: ReactNode;
  title: string;
  description: string;
};

export function OptionToggle({ id, checked, onChange, disabled, icon, title, description }: SwitchProps) {
  return (
    <label className="toggle-row" htmlFor={id}>
      <span className="toggle-icon">{icon}</span>
      <span className="toggle-copy">
        <strong>{title}</strong>
        <span>{description}</span>
      </span>
      <span className="switch">
        <input
          id={id}
          type="checkbox"
          role="switch"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="switch-track" />
        <span className="switch-thumb" />
      </span>
    </label>
  );
}
