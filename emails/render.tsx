/**
 * CLI renderer — called by email_service.py via subprocess.
 * Usage: tsx emails/render.tsx <template_name> <json_data>
 * Outputs rendered HTML to stdout.
 */
import * as React from "react";
import { render } from "@react-email/render";

import Welcome from "./templates/Welcome";
import BookingConfirmation from "./templates/BookingConfirmation";
import BookingCancellation from "./templates/BookingCancellation";
import PasswordReset from "./templates/PasswordReset";

const TEMPLATES: Record<string, React.FC<any>> = {
  welcome:              Welcome,
  booking_confirmation: BookingConfirmation,
  booking_cancellation: BookingCancellation,
  password_reset:       PasswordReset,
};

async function main() {
  const templateName = process.argv[2];
  const rawData = process.argv[3] ?? "{}";

  if (!templateName) {
    process.stderr.write("Usage: render.tsx <template> <json>\n");
    process.exit(1);
  }

  const Template = TEMPLATES[templateName];
  if (!Template) {
    process.stderr.write(`Unknown template: "${templateName}". Available: ${Object.keys(TEMPLATES).join(", ")}\n`);
    process.exit(1);
  }

  let data: Record<string, unknown>;
  try {
    data = JSON.parse(rawData);
  } catch {
    process.stderr.write(`Invalid JSON data: ${rawData}\n`);
    process.exit(1);
  }

  const html = await render(<Template {...data} />);
  process.stdout.write(html);
}

main().catch((err) => {
  process.stderr.write(`Render error: ${err?.message ?? err}\n`);
  process.exit(1);
});
