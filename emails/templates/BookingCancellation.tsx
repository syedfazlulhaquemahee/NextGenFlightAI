import * as React from "react";
import { Layout } from "../components/Layout";
import {
  HighlightBox,
  DetailTable,
  CtaButton,
} from "../components/DetailTable";
import { color, font } from "../components/theme";

export interface BookingCancellationProps {
  bookingReference?: string;
  airlineName?: string;
  hasRefund?: boolean;
  refundAmount?: string;
  refundCurrency?: string;
  searchUrl?: string;
  supportEmail?: string;
  brand?: string;
}

export default function BookingCancellation({
  bookingReference,
  airlineName,
  hasRefund = false,
  refundAmount = "",
  refundCurrency = "USD",
  searchUrl,
  supportEmail,
  brand = "Skairova",
}: BookingCancellationProps) {
  const ref = bookingReference?.trim() || "—";

  const subtitle = hasRefund
    ? `A refund of ${refundCurrency} ${refundAmount} will be returned to your original payment method.`
    : "Your cancellation has been processed.";

  const detailRows = [
    ...(airlineName ? [{ label: "Airline", value: airlineName }] : []),
    { label: "Status", value: "Cancelled" },
  ];

  return (
    <Layout
      preheader={
        `Booking ${ref} cancelled.` +
        (hasRefund ? ` Refund of ${refundCurrency} ${refundAmount} is on its way.` : "")
      }
      accentColor={color.red}
      accentTint={color.redTint}
      title="Your booking has been cancelled."
      subtitle={subtitle}
      supportEmail={supportEmail}
    >
      {/* Reference */}
      <HighlightBox
        label="Cancelled booking reference"
        value={ref}
        bg={color.redBg}
        borderColor={color.redBorder}
        labelColor={color.redText}
      />

      {/* Cancellation details */}
      <DetailTable label="Cancellation details" rows={detailRows} />

      {/* Refund block */}
      {hasRefund ? (
        <table
          role="presentation"
          cellPadding={0}
          cellSpacing={0}
          style={{ width: "100%", marginBottom: "32px" }}
        >
          <tbody>
            <tr>
              <td
                style={{
                  background: color.greenBg,
                  border: `1px solid ${color.greenBorder}`,
                  borderRadius: "8px",
                  padding: "20px 22px",
                }}
              >
                <div style={refundLabel}>Refund</div>
                <div style={refundAmount_}>
                  {refundCurrency} {refundAmount}
                </div>
                <div style={refundNote}>
                  Will be returned to your original payment method within
                  5&ndash;10 business days.
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      ) : (
        <table
          role="presentation"
          cellPadding={0}
          cellSpacing={0}
          style={{ width: "100%", marginBottom: "32px" }}
        >
          <tbody>
            <tr>
              <td
                style={{
                  background: color.redBg,
                  border: `1px solid ${color.redBorder}`,
                  borderRadius: "8px",
                  padding: "16px 20px",
                }}
              >
                <p style={nonRefundText}>
                  This booking was non-refundable. No refund will be issued.
                </p>
              </td>
            </tr>
          </tbody>
        </table>
      )}

      {searchUrl && (
        <CtaButton href={searchUrl} label="Search new flights" bg={color.link} />
      )}
    </Layout>
  );
}

const refundLabel: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "11px",
  fontWeight: 600,
  textTransform: "uppercase",
  letterSpacing: "0.08em",
  color: color.greenText,
  marginBottom: "8px",
};

const refundAmount_: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: "26px",
  fontWeight: 700,
  color: color.heading,
  lineHeight: 1,
  marginBottom: "8px",
};

const refundNote: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "13px",
  color: color.muted,
  lineHeight: 1.55,
};

const nonRefundText: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "13px",
  fontWeight: 500,
  color: color.redBody,
  lineHeight: 1.55,
  margin: 0,
};
