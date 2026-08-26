import * as React from "react";
import { Layout } from "../components/Layout";
import { CtaButton } from "../components/DetailTable";
import { color, font } from "../components/theme";

export interface WelcomeProps {
  firstName?: string;
  portalUrl?: string;
  searchUrl?: string;
  supportEmail?: string;
  brand?: string;
}

const features: { num: string; title: string; desc: string }[] = [
  {
    num: "01",
    title: "Search in plain English",
    desc: "Tell us where and when — \"cheapest week to Lisbon in June\" works just as well as picking dates from a calendar.",
  },
  {
    num: "02",
    title: "See the real price, upfront",
    desc: "Live fares straight from the airlines. The price you're quoted is the price you pay — no hidden fees.",
  },
  {
    num: "03",
    title: "Every trip, saved automatically",
    desc: "Itineraries and receipts land in your account the moment you book, ready whenever you need them.",
  },
];

export default function Welcome({
  firstName,
  portalUrl,
  searchUrl,
  supportEmail,
  brand = "Skairova",
}: WelcomeProps) {
  const greeting = firstName?.trim() || "there";
  // The "aha moment" for a flight-search product is searching, not visiting
  // the account page — so that's the primary CTA target whenever we have it.
  const ctaUrl = searchUrl || portalUrl || "#";

  return (
    <Layout
      preheader={`Welcome to ${brand} — your account is ready. Let's find your next flight.`}
      accentColor={color.indigo}
      accentTint={color.indigoTint}
      title={`Welcome, ${greeting}.`}
      subtitle={`Your account is ready. Let's find your next flight.`}
      supportEmail={supportEmail}
    >
      {/* Feature rows */}
      <table
        role="presentation"
        cellPadding={0}
        cellSpacing={0}
        style={{ width: "100%", marginBottom: "32px" }}
      >
        <tbody>
          {features.map((f, i) => (
            <tr key={i}>
              <td
                style={{
                  paddingBottom: i < features.length - 1 ? "20px" : "0",
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
                      {/* Left accent border cell */}
                      <td
                        style={{
                          width: "3px",
                          backgroundColor: color.indigo,
                          borderRadius: "2px",
                          verticalAlign: "stretch",
                          paddingRight: "0",
                        }}
                      />
                      {/* Spacer */}
                      <td style={{ width: "16px" }} />
                      {/* Content */}
                      <td style={{ verticalAlign: "top" }}>
                        <div style={numLabel}>{f.num}</div>
                        <div style={featureTitle}>{f.title}</div>
                        <div style={featureDesc}>{f.desc}</div>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* Divider before CTA */}
      <table
        role="presentation"
        cellPadding={0}
        cellSpacing={0}
        style={{ width: "100%", marginBottom: "24px" }}
      >
        <tbody>
          <tr>
            <td
              style={{
                height: "1px",
                backgroundColor: color.border,
                fontSize: "0",
                lineHeight: "0",
              }}
            >
              &nbsp;
            </td>
          </tr>
        </tbody>
      </table>

      <CtaButton href={ctaUrl} label="Search flights" bg={color.indigo} />

      <div style={signoff}>Happy travels — the {brand} team.</div>
    </Layout>
  );
}

const numLabel: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: "11px",
  fontWeight: 700,
  color: color.indigo,
  letterSpacing: "0.06em",
  marginBottom: "4px",
};

const featureTitle: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "14px",
  fontWeight: 600,
  color: color.heading,
  marginBottom: "4px",
  lineHeight: 1.4,
};

const featureDesc: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "13px",
  color: color.muted,
  lineHeight: 1.6,
};

const signoff: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "13px",
  color: color.muted,
  marginTop: "20px",
};
