/**
 * The three-step rail above the panels: upload, extract, audit.
 *
 * The states are derived from the selected document, so the rail doubles as a
 * progress indicator for whatever row is currently open.
 */

export type StepState = "idle" | "active" | "done" | "blocked";

export interface Step {
  title: string;
  note: string;
  state: StepState;
}

export function StepRail({ steps }: { steps: Step[] }) {
  return (
    <section className="steps" aria-label="Workflow progress">
      {steps.map((step, index) => (
        <div className="step" data-state={step.state} key={step.title}>
          <span className="step-no" aria-hidden="true">
            {step.state === "done" ? "✓" : index + 1}
          </span>
          <span>
            <span className="step-title">{step.title}</span>
            <span className="step-note" style={{ display: "block" }}>
              {step.note}
            </span>
          </span>
        </div>
      ))}
    </section>
  );
}
