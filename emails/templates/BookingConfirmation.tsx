import * as React from "react";
import { Layout } from "../components/Layout";
import {
  HighlightBox,
  DetailTable,
  SectionLabel,
  CtaButton,
} from "../components/DetailTable";
import { color, font } from "../components/theme";

export interface BookingSegment {
  carrierName?: string;
  flightNumber?: string;
  aircraft?: string;
  originCode?: string;
  destinationCode?: string;
  originCity?: string;
  destinationCity?: string;
  departTime?: string;
  arriveTime?: string;
  departDateFull?: string;
  arriveDateFull?: string;
  departureTerminal?: string;
  arrivalTerminal?: string;
  durationLabel?: string;
}

export interface BookingSlice {
  label: string;
  route: string;
  departLabel?: string;
  arriveLabel?: string;
  durationLabel?: string;
  stopsLabel?: string;
  segments?: BookingSegment[];
}

export interface BookingConfirmationProps {
  bookingReference?: string;
  airlineName?: string;
  currency?: string;
  totalAmount?: string;
  passengerNames?: string[];
  slices?: BookingSlice[];
  manageUrl?: string;
  supportEmail?: string;
  brand?: string;
}

export default function BookingConfirmation({
  bookingReference,
  airlineName = "Your airline",
  currency = "USD",
  totalAmount = "0.00",
  passengerNames = [],
  slices = [],
  manageUrl,
  supportEmail,
  brand = "Skairova",
}: BookingConfirmationProps) {
  const ref = bookingReference?.trim() || "—";
  const paxList = passengerNames.filter(Boolean).join(", ") || "See your portal";

  const detailRows = [
    { label: "Airline", value: airlineName },
    { label: "Total paid", value: `${currency} ${totalAmount}` },
    ...(passengerNames.length > 0
      ? [{ label: passengerNames.length === 1 ? "Passenger" : "Passengers", value: paxList }]
      : []),
  ];

  return (
    <Layout
      preheader={`Booking ${ref} confirmed. Have a great flight.`}
      accentColor={color.green}
      accentTint={color.greenTint}
      title="Your booking is confirmed."
      subtitle={`Your flight with ${airlineName} is booked. Keep this email for your records.`}
      supportEmail={supportEmail}
    >
      {/* Reference */}
      <HighlightBox
        label="Booking reference"
        value={ref}
        bg={color.greenBg}
        borderColor={color.greenBorder}
        labelColor={color.greenText}
      />

      {/* Booking details */}
      <DetailTable label="Booking details" rows={detailRows} />

      {/* Itinerary */}
      {slices.length > 0 && (
        <div style={{ marginBottom: "32px" }}>
          <SectionLabel>Itinerary</SectionLabel>
          {slices.map((sl, si) => (
            <SliceBlock key={si} slice={sl} isLast={si === slices.length - 1} />
          ))}
        </div>
      )}

      {manageUrl && (
        <CtaButton href={manageUrl} label="Manage booking" bg={color.green} />
      )}
    </Layout>
  );
}

function SliceBlock({ slice, isLast }: { slice: BookingSlice; isLast: boolean }) {
  const segments = slice.segments ?? [];
  const meta = [slice.durationLabel, slice.stopsLabel].filter(Boolean).join(" · ");

  return (
    <table
      role="presentation"
      cellPadding={0}
      cellSpacing={0}
      style={{
        width: "100%",
        marginBottom: isLast ? "0" : "20px",
        border: `1px solid ${color.border}`,
        borderRadius: "8px",
        borderCollapse: "separate",
        borderSpacing: 0,
        overflow: "hidden",
      }}
    >
      <tbody>
        {/* Slice header row */}
        <tr>
          <td
            style={{
              backgroundColor: color.rule,
              borderBottom: `1px solid ${color.border}`,
              padding: "10px 18px",
              borderRadius: "8px 8px 0 0",
            }}
          >
            <table role="presentation" cellPadding={0} cellSpacing={0} style={{ width: "100%" }}>
              <tbody>
                <tr>
                  <td>
                    <span style={sliceHeaderLabel}>{slice.label}</span>
                    {meta && <span style={sliceHeaderMeta}>&nbsp;&nbsp;{meta}</span>}
                  </td>
                </tr>
              </tbody>
            </table>
          </td>
        </tr>

        {/* Segments */}
        {segments.length > 0 ? (
          segments.map((seg, i) => (
            <tr key={i}>
              <td
                style={{
                  padding: "18px 18px",
                  borderBottom: i < segments.length - 1 ? `1px solid ${color.rule}` : "none",
                  borderRadius: i === segments.length - 1 ? "0 0 8px 8px" : undefined,
                }}
              >
                <SegmentContent seg={seg} />
              </td>
            </tr>
          ))
        ) : (
          <tr>
            <td style={{ padding: "16px 18px", borderRadius: "0 0 8px 8px" }}>
              <SliceRouteRow
                route={slice.route}
                departLabel={slice.departLabel}
                arriveLabel={slice.arriveLabel}
              />
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

function SegmentContent({ seg }: { seg: BookingSegment }) {
  const origin = seg.originCode || "—";
  const dest   = seg.destinationCode || "—";

  return (
    <>
      {/* Carrier row */}
      <table role="presentation" cellPadding={0} cellSpacing={0} style={{ width: "100%", marginBottom: "14px" }}>
        <tbody>
          <tr>
            <td>
              <span style={carrierName}>{seg.carrierName || "Airline"}</span>
              {seg.flightNumber && (
                <span style={flightNum}>&nbsp;&nbsp;·&nbsp;&nbsp;{seg.flightNumber}</span>
              )}
            </td>
            {seg.aircraft && (
              <td style={{ textAlign: "right" }}>
                <span style={aircraftLabel}>{seg.aircraft}</span>
              </td>
            )}
          </tr>
        </tbody>
      </table>

      {/* Route row: ORIGIN — arrow — DESTINATION */}
      <table role="presentation" cellPadding={0} cellSpacing={0} style={{ width: "100%" }}>
        <tbody>
          <tr>
            {/* Origin */}
            <td style={{ verticalAlign: "top" }}>
              <div style={iataCode}>{origin}</div>
              {seg.originCity && <div style={cityLabel}>{seg.originCity}</div>}
              {seg.departTime && <div style={timeLabel}>{seg.departTime}</div>}
              {seg.departDateFull && <div style={dateLabel}>{seg.departDateFull}</div>}
              {seg.departureTerminal && (
                <div style={termLabel}>Terminal {seg.departureTerminal}</div>
              )}
            </td>

            {/* Arrow + duration — pure table cell, no flex */}
            <td
              style={{
                width: "80px",
                textAlign: "center",
                verticalAlign: "middle",
                padding: "0 6px",
              }}
            >
              <div style={arrowChar}>&#x2192;</div>
              {seg.durationLabel && (
                <div style={durationLabel}>{seg.durationLabel}</div>
              )}
            </td>

            {/* Destination */}
            <td style={{ verticalAlign: "top", textAlign: "right" }}>
              <div style={iataCode}>{dest}</div>
              {seg.destinationCity && <div style={cityLabel}>{seg.destinationCity}</div>}
              {seg.arriveTime && <div style={timeLabel}>{seg.arriveTime}</div>}
              {seg.arriveDateFull && <div style={dateLabel}>{seg.arriveDateFull}</div>}
              {seg.arrivalTerminal && (
                <div style={{ ...termLabel, textAlign: "right" }}>
                  Terminal {seg.arrivalTerminal}
                </div>
              )}
            </td>
          </tr>
        </tbody>
      </table>
    </>
  );
}

function SliceRouteRow({
  route,
  departLabel,
  arriveLabel,
}: {
  route: string;
  departLabel?: string;
  arriveLabel?: string;
}) {
  return (
    <>
      <div style={{ fontFamily: font.sans, fontSize: "15px", fontWeight: 600, color: color.heading, marginBottom: "4px" }}>
        {route.replace("->", "→")}
      </div>
      {(departLabel || arriveLabel) && (
        <div style={{ fontFamily: font.sans, fontSize: "13px", color: color.muted }}>
          {[departLabel && `Departs ${departLabel}`, arriveLabel && `Arrives ${arriveLabel}`]
            .filter(Boolean)
            .join(" · ")}
        </div>
      )}
    </>
  );
}

// ── Styles ──────────────────────────────────────────────────────────────────

const sliceHeader: React.CSSProperties = {
  backgroundColor: color.rule,
  borderBottom: `1px solid ${color.border}`,
  padding: "10px 18px",
  display: "flex" as const,
  alignItems: "center",
  justifyContent: "space-between",
};

const sliceHeaderLabel: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "11px",
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: "0.08em",
  color: color.muted,
};

const sliceHeaderMeta: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "12px",
  color: color.faint,
};

const carrierName: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "13px",
  fontWeight: 600,
  color: color.heading,
};

const flightNum: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: "12px",
  color: color.muted,
};

const aircraftLabel: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "12px",
  color: color.faint,
};

const iataCode: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: "22px",
  fontWeight: 700,
  color: color.heading,
  letterSpacing: "0.04em",
  lineHeight: 1,
  marginBottom: "3px",
};

const cityLabel: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "12px",
  color: color.muted,
  marginBottom: "6px",
};

const timeLabel: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "15px",
  fontWeight: 600,
  color: color.heading,
};

const dateLabel: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "12px",
  color: color.muted,
};

const termLabel: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "11px",
  color: color.faint,
  marginTop: "3px",
};

const arrowChar: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "20px",
  color: color.muted,
  lineHeight: 1,
};

const durationLabel: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "11px",
  color: color.faint,
  textAlign: "center",
};
