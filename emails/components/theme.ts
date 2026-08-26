export const font = {
  sans: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
  mono: "'SFMono-Regular', Menlo, Monaco, Consolas, 'Courier New', monospace",
} as const;

export const color = {
  outer:       "#f0f2f5",
  card:        "#ffffff",
  border:      "#e2e8f0",
  rule:        "#f1f5f9",
  heading:     "#0f172a",
  body:        "#334155",
  muted:       "#64748b",
  faint:       "#94a3b8",
  link:        "#4f6fff",

  // accent bars (one per email type)
  indigo:      "#4f46e5",
  green:       "#059669",
  red:         "#dc2626",
  blue:        "#2563eb",

  // header tints
  indigoTint:  "#f5f4ff",
  greenTint:   "#f0fdf8",
  redTint:     "#fff8f8",
  blueTint:    "#f0f6ff",

  // tinted boxes
  greenBg:     "#f0fdf4",
  greenBorder: "#bbf7d0",
  greenText:   "#16a34a",
  redBg:       "#fff1f2",
  redBorder:   "#fecdd3",
  redText:     "#e11d48",
  redBody:     "#9f1239",
} as const;
