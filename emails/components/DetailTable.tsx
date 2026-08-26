import * as React from "react";
import { Row, Column, Section } from "@react-email/components";
import { color, font } from "./theme";

export interface DetailRowData {
  label: string;
  value: string;
  mono?: boolean;
}

interface DetailTableProps {
  rows: DetailRowData[];
  label?: string;
  style?: React.CSSProperties;
}

export function SectionLabel({ children }: { children: string }) {
  return (
    <div style={labelStyle}>{children}</div>
  );
}

export function DetailTable({ rows, label, style }: DetailTableProps) {
  return (
    <div style={{ marginBottom: "28px", ...style }}>
      {label && <SectionLabel>{label}</SectionLabel>}
      <table
        role="presentation"
        cellPadding={0}
        cellSpacing={0}
        style={{ width: "100%", borderTop: `1px solid ${color.border}` }}
      >
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              <td
                style={{
                  ...rowCell,
                  borderBottom:
                    i < rows.length - 1 ? `1px solid ${color.rule}` : "none",
                }}
              >
                <table
                  role="presentation"
                  cellPadding={0}
                  cellSpacing={0}
                  style={{ width: "100%" }}
                >
                  <tbody>
                    <tr>
                      <td style={keyCell}>{row.label}</td>
                      <td
                        style={{
                          ...valueCell,
                          fontFamily: row.mono ? font.mono : font.sans,
                          fontSize: row.mono ? "15px" : "14px",
                          letterSpacing: row.mono ? "0.07em" : undefined,
                        }}
                      >
                        {row.value}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function HighlightBox({
  label,
  value,
  note,
  bg,
  borderColor,
  labelColor,
  valueFamily = font.mono,
}: {
  label: string;
  value: string;
  note?: string;
  bg: string;
  borderColor: string;
  labelColor: string;
  valueFamily?: string;
}) {
  return (
    <table
      role="presentation"
      cellPadding={0}
      cellSpacing={0}
      style={{ width: "100%", marginBottom: "28px" }}
    >
      <tbody>
        <tr>
          <td
            style={{
              background: bg,
              border: `1px solid ${borderColor}`,
              borderRadius: "8px",
              padding: "20px 22px",
            }}
          >
            <div
              style={{
                fontFamily: font.sans,
                fontSize: "11px",
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.08em",
                color: labelColor,
                marginBottom: "8px",
              }}
            >
              {label}
            </div>
            <div
              style={{
                fontFamily: valueFamily,
                fontSize: "26px",
                fontWeight: 700,
                color: color.heading,
                letterSpacing: "0.08em",
                lineHeight: 1,
                marginBottom: note ? "8px" : undefined,
              }}
            >
              {value}
            </div>
            {note && (
              <div
                style={{
                  fontFamily: font.sans,
                  fontSize: "13px",
                  color: color.muted,
                  lineHeight: 1.55,
                }}
              >
                {note}
              </div>
            )}
          </td>
        </tr>
      </tbody>
    </table>
  );
}

export function CtaButton({
  href,
  label,
  bg = color.link,
}: {
  href: string;
  label: string;
  bg?: string;
}) {
  return (
    <table
      role="presentation"
      cellPadding={0}
      cellSpacing={0}
      style={{ marginTop: "8px" }}
    >
      <tbody>
        <tr>
          <td
            style={{
              borderRadius: "7px",
              backgroundColor: bg,
            }}
          >
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                display: "inline-block",
                padding: "13px 26px",
                fontFamily: font.sans,
                fontSize: "14px",
                fontWeight: 600,
                color: "#ffffff",
                textDecoration: "none",
                borderRadius: "7px",
                letterSpacing: "0.01em",
                lineHeight: 1,
              }}
            >
              {label}
            </a>
          </td>
        </tr>
      </tbody>
    </table>
  );
}

const labelStyle: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "11px",
  fontWeight: 600,
  textTransform: "uppercase",
  letterSpacing: "0.08em",
  color: color.muted,
  marginBottom: "14px",
};

const rowCell: React.CSSProperties = {
  padding: "12px 0",
};

const keyCell: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "13px",
  fontWeight: 500,
  color: color.muted,
  width: "44%",
  paddingRight: "16px",
  verticalAlign: "top",
};

const valueCell: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "14px",
  fontWeight: 600,
  color: color.heading,
  textAlign: "right",
  verticalAlign: "top",
};
