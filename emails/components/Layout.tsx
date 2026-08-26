import * as React from "react";
import {
  Html,
  Head,
  Preview,
  Body,
  Container,
  Section,
  Row,
  Column,
  Hr,
  Text,
} from "@react-email/components";
import { color, font } from "./theme";

interface LayoutProps {
  preheader: string;
  accentColor: string;
  accentTint: string;
  title: string;
  subtitle: string | React.ReactNode;
  supportEmail?: string;
  children: React.ReactNode;
}

export function Layout({
  preheader,
  accentColor,
  accentTint,
  title,
  subtitle,
  supportEmail,
  children,
}: LayoutProps) {
  const brand = "Skairova";

  return (
    <Html lang="en">
      <Head />
      <Preview>{preheader}</Preview>
      <Body style={styles.body}>
        <Container style={styles.container}>

          {/* Accent bar */}
          <div style={{ ...styles.accentBar, backgroundColor: accentColor }} />

          {/* Header: logo + heading unified in a tinted block */}
          <Section style={{ ...styles.headerSection, backgroundColor: accentTint }}>
            <Row>
              <Column>
                <div style={styles.logoRow}>
                  <span style={styles.logoSkai}>SKAI</span>
                  <span style={{ ...styles.logoRova, color: accentColor }}>ROVA</span>
                </div>
                <Text style={styles.title}>{title}</Text>
                {typeof subtitle === "string" ? (
                  <Text style={styles.subtitle}>{subtitle}</Text>
                ) : (
                  subtitle
                )}
              </Column>
            </Row>
          </Section>

          {/* Body content */}
          <Section style={styles.bodySection}>{children}</Section>

          {/* Footer */}
          <Section style={styles.footer}>
            {supportEmail && (
              <Text style={styles.footerContact}>
                Questions? Contact us at{" "}
                <a href={`mailto:${supportEmail}`} style={{ ...styles.footerLink, color: accentColor }}>
                  {supportEmail}
                </a>
                .
              </Text>
            )}
            <Text style={styles.footerText}>
              &copy; {brand} &nbsp;&middot;&nbsp; This is an automated message
              — please do not reply directly.
            </Text>
          </Section>

        </Container>
      </Body>
    </Html>
  );
}

const styles: Record<string, React.CSSProperties> = {
  body: {
    margin: 0,
    padding: 0,
    backgroundColor: color.outer,
    WebkitTextSizeAdjust: "100%",
    MsTextSizeAdjust: "100%",
  },
  container: {
    maxWidth: "560px",
    margin: "44px auto 60px",
    backgroundColor: color.card,
    border: `1px solid ${color.border}`,
    borderRadius: "10px",
    overflow: "hidden",
    boxShadow: "0 2px 12px rgba(15,23,42,0.08)",
  },
  accentBar: {
    height: "4px",
    width: "100%",
    display: "block",
  },
  headerSection: {
    padding: "28px 36px 32px",
    borderBottom: `1px solid ${color.border}`,
  },
  logoRow: {
    marginBottom: "28px",
  },
  logoSkai: {
    fontFamily: font.sans,
    fontSize: "17px",
    fontWeight: 800,
    letterSpacing: "-0.04em",
    color: color.heading,
  },
  logoRova: {
    fontFamily: font.sans,
    fontSize: "17px",
    fontWeight: 800,
    letterSpacing: "-0.04em",
  },
  title: {
    fontFamily: font.sans,
    fontSize: "28px",
    fontWeight: 700,
    color: color.heading,
    lineHeight: "1.25",
    letterSpacing: "-0.025em",
    margin: "0 0 10px",
  },
  subtitle: {
    fontFamily: font.sans,
    fontSize: "15px",
    color: color.body,
    lineHeight: "1.65",
    margin: "0",
  },
  bodySection: {
    padding: "32px 36px 36px",
  },
  footer: {
    backgroundColor: color.rule,
    borderTop: `1px solid ${color.border}`,
    padding: "20px 36px 24px",
  },
  footerContact: {
    fontFamily: font.sans,
    fontSize: "13px",
    color: color.muted,
    lineHeight: "1.6",
    margin: "0 0 8px",
  },
  footerLink: {
    textDecoration: "none",
  },
  footerText: {
    fontFamily: font.sans,
    fontSize: "12px",
    color: color.faint,
    lineHeight: "1.6",
    margin: "0",
  },
};
