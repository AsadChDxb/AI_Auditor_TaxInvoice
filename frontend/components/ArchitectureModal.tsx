"use client";

/**
 * "Architecture": the header button's diagram.
 *
 * One visual map of a single request — browser → API (read the PDF, quality gate,
 * optional OCR) → AI extraction → deterministic VAT rules → AI review → report JSON back
 * to the browser. Boxes are colour-coded by owner (client / API / AI / business logic) and
 * every connector carries a bright segment that travels along it, so the data path reads
 * like current through a circuit. No prose: the diagram is the whole explanation.
 *
 * Motion is CSS only and is switched off under `prefers-reduced-motion`.
 */

import { useEffect } from "react";

const LAYERS = {
  client: { fill: "#eef2ff", stroke: "#c7d2fe", tag: "#4338ca", label: "CLIENT" },
  api: { fill: "#f8fafc", stroke: "#cbd5e1", tag: "#475569", label: "API" },
  ai: { fill: "#f0f9ff", stroke: "#bae6fd", tag: "#0369a1", label: "AI" },
  rules: { fill: "#ecfdf5", stroke: "#a7f3d0", tag: "#047857", label: "BUSINESS LOGIC" },
} as const;

type Layer = keyof typeof LAYERS;

/** One box of the diagram: layer tag, title, sub-label and an optional step number. */
function DiagramNode({
  x,
  y,
  width = 290,
  height = 92,
  layer,
  step,
  title,
  sub,
}: {
  x: number;
  y: number;
  width?: number;
  height?: number;
  layer: Layer;
  step?: number;
  title: string;
  sub: string;
}) {
  const palette = LAYERS[layer];

  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        rx={14}
        fill={palette.fill}
        stroke={palette.stroke}
        strokeWidth={1.5}
      />
      <text
        x={x + 16}
        y={y + 23}
        fill={palette.tag}
        fontSize={9}
        fontWeight={700}
        letterSpacing="0.08em"
      >
        {palette.label}
      </text>
      {step === undefined ? null : (
        <g>
          <circle
            cx={x + width - 22}
            cy={y + 21}
            r={11}
            fill="#ffffff"
            stroke={palette.stroke}
            strokeWidth={1.5}
          />
          <text
            x={x + width - 22}
            y={y + 25}
            textAnchor="middle"
            fill={palette.tag}
            fontSize={11}
            fontWeight={700}
          >
            {step}
          </text>
        </g>
      )}
      <text x={x + 16} y={y + 50} fill="#0f172a" fontSize={15} fontWeight={700}>
        {title}
      </text>
      <text x={x + 16} y={y + 70} fill="#64748b" fontSize={10.5}>
        {sub}
      </text>
    </g>
  );
}

/** A connector: a static track plus the travelling "current" segment on top of it. */
function Flow({ d, delay = 0 }: { d: string; delay?: number }) {
  return (
    <g>
      <path className="diagram-track" d={d} markerEnd="url(#diagram-arrow)" />
      <path
        className="diagram-flow"
        d={d}
        pathLength={100}
        style={{ animationDelay: `${delay}s` }}
      />
    </g>
  );
}

export function ArchitectureModal({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose]);

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Architecture diagram"
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div className="modal arch-modal">
        <header className="modal-head">
          <h2 className="card-title">Architecture</h2>
          <div className="row-wrap">
            <span className="arch-live">
              <span className="arch-live-dot" aria-hidden="true" />
              data in flight
            </span>
            <button type="button" className="btn btn-sm" onClick={onClose} autoFocus>
              Close (Esc)
            </button>
          </div>
        </header>
        <div className="modal-body arch-body">
          <div className="arch-canvas">
            <svg
              viewBox="0 0 1160 470"
              role="img"
              aria-label="Request flow: the browser sends the PDF to the API, the API reads text and tables and falls back to OCR for image-only pages, the AI extracts the invoice fields, the business-logic rules check VAT, the AI review adds findings, and one JSON response returns to the browser."
            >
              <defs>
                <marker
                  id="diagram-arrow"
                  viewBox="0 0 10 10"
                  refX="9"
                  refY="5"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
                </marker>
              </defs>

              {/* 1 → 2 → 3 across the top, down the right side, then 4 → 5 → 6 back. */}
              <Flow d="M 380 98 H 457" />
              <Flow d="M 750 98 H 827" delay={0.5} />
              <Flow d="M 975 144 V 313" delay={1} />
              <Flow d="M 830 362 H 753" delay={1.5} />
              <Flow d="M 460 362 H 383" delay={2} />
              <Flow
                d="M 88 362 H 56 Q 44 362 44 350 V 110 Q 44 98 56 98 H 88"
                delay={0.8}
              />

              {/* Optional OCR branch, only for image-only pages. */}
              <path
                className="diagram-track-dashed"
                d="M 605 144 V 187"
                markerEnd="url(#diagram-arrow)"
              />
              <path
                className="diagram-track-dashed"
                d="M 730 228 Q 850 228 936 152"
                markerEnd="url(#diagram-arrow)"
              />

              <DiagramNode
                x={90}
                y={52}
                layer="client"
                step={1}
                title="Browser UI"
                sub="upload · results"
              />
              <DiagramNode
                x={460}
                y={52}
                layer="api"
                step={2}
                title="Read PDF"
                sub="text + tables · gate"
              />
              <DiagramNode
                x={830}
                y={52}
                layer="ai"
                step={3}
                title="AI extraction"
                sub="fields + confidence"
              />
              <DiagramNode
                x={830}
                y={316}
                layer="rules"
                step={4}
                title="VAT rules"
                sub="6 deterministic checks"
              />
              <DiagramNode
                x={460}
                y={316}
                layer="ai"
                step={5}
                title="AI review"
                sub="evidence · configured rules"
              />
              <DiagramNode
                x={90}
                y={316}
                layer="api"
                step={6}
                title="Report"
                sub="status · confidence"
              />

              {/* The OCR fallback box (dashed: optional). */}
              <g>
                <rect
                  x={480}
                  y={190}
                  width={250}
                  height={76}
                  rx={12}
                  fill="#f8fafc"
                  stroke="#94a3b8"
                  strokeWidth={1.5}
                  strokeDasharray="6 5"
                />
                <text
                  x={496}
                  y={212}
                  fill="#475569"
                  fontSize={9}
                  fontWeight={700}
                  letterSpacing="0.08em"
                >
                  OPTIONAL
                </text>
                <text x={496} y={236} fill="#0f172a" fontSize={14} fontWeight={700}>
                  OCR fallback
                </text>
                <text x={496} y={254} fill="#64748b" fontSize={10.5}>
                  image-only pages
                </text>
              </g>

              {/* What travels between the boxes. */}
              <text className="diagram-label" x={419} y={88} textAnchor="middle">
                PDF
              </text>
              <text className="diagram-label" x={789} y={88} textAnchor="middle">
                pages
              </text>
              <text className="diagram-label" x={986} y={236}>
                fields
              </text>
              <text className="diagram-label" x={792} y={352} textAnchor="middle">
                findings
              </text>
              <text className="diagram-label" x={421} y={352} textAnchor="middle">
                confidence
              </text>
              <text
                className="diagram-label"
                x={62}
                y={230}
                textAnchor="middle"
                transform="rotate(-90 62 230)"
              >
                json
              </text>
              <text className="diagram-label" x={612} y={172}>
                thin page
              </text>
              <text className="diagram-label" x={851} y={252} textAnchor="middle">
                recovered text
              </text>

              <text className="diagram-caption" x={580} y={446} textAnchor="middle">
                1 request → 1 response
              </text>
            </svg>
          </div>

          <div className="diagram-legend">
            <span>
              <span className="diagram-dot diagram-dot-client" aria-hidden="true" />
              Client
            </span>
            <span>
              <span className="diagram-dot diagram-dot-api" aria-hidden="true" />
              API
            </span>
            <span>
              <span className="diagram-dot diagram-dot-ai" aria-hidden="true" />
              AI
            </span>
            <span>
              <span className="diagram-dot diagram-dot-rules" aria-hidden="true" />
              Business logic
            </span>
          </div>

          <div className="arch-boundary">
            <span className="arch-boundary-side arch-boundary-ai">
              <strong>AI</strong>
              <span>fields + confidence</span>
            </span>
            <span className="arch-boundary-arrow" aria-hidden="true">
              <span className="arch-spark" />
            </span>
            <span className="arch-boundary-side arch-boundary-rules">
              <strong>Business logic</strong>
              <span>recomputes VAT · decides status</span>
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
