import * as React from "react";
import { Text } from "@react-email/components";
import { Layout } from "../components/Layout";
import { color, font } from "../components/theme";

export interface PasswordResetProps {
  firstName?: string;
  code?: string;
  expiryMinutes?: number;
  supportEmail?: string;
  brand?: string;
}

export default function PasswordReset({
  firstName,
  code = "",
  expiryMinutes = 10,
  supportEmail,
  brand = "Skairova",
}: PasswordResetProps) {
  const greeting = firstName?.trim() || "there";
  const expiry = Math.max(1, expiryMinutes);
  const spaced = code.length <= 8 ? code.split("").join("  ") : code;

  return (
    <Layout
      preheader={`Your verification code is ${code}. Expires in ${expiry} minutes.`}
      accentColor={color.blue}
      accentTint={color.blueTint}
      title="Verify your identity."
      subtitle={`Enter the code below to complete your password reset on ${brand}.`}
      supportEmail={supportEmail}
    >
      {/* OTP box */}
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
                backgroundColor: color.rule,
                border: `1px solid ${color.border}`,
                borderRadius: "8px",
                padding: "34px 20px",
                textAlign: "center",
              }}
            >
              <div style={codeDisplay}>{spaced}</div>
              <div style={expireNote}>
                Expires in{" "}
                <strong style={{ color: color.body }}>{expiry} minutes</strong>
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      {/* Security note */}
      <table
        role="presentation"
        cellPadding={0}
        cellSpacing={0}
        style={{ width: "100%" }}
      >
        <tbody>
          <tr>
            <td
              style={{
                borderLeft: `3px solid ${color.border}`,
                paddingLeft: "16px",
              }}
            >
              <p style={securityNote}>
                If you did not request a password reset, you can ignore this
                email. Your account and password will remain unchanged.
              </p>
            </td>
          </tr>
        </tbody>
      </table>
    </Layout>
  );
}

const body: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "15px",
  color: color.body,
  lineHeight: 1.65,
  margin: "0 0 28px",
};

const codeDisplay: React.CSSProperties = {
  fontFamily: font.mono,
  fontSize: "40px",
  fontWeight: 700,
  color: color.heading,
  letterSpacing: "0.22em",
  lineHeight: 1,
};

const expireNote: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "13px",
  color: color.muted,
  marginTop: "14px",
};

const securityNote: React.CSSProperties = {
  fontFamily: font.sans,
  fontSize: "13px",
  color: color.muted,
  lineHeight: 1.6,
  margin: 0,
};
