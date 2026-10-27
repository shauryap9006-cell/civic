"""Email templates for CivicLens authority notifications.

Implements RPT-01..04, TC-RPT-04, and W-06.
Guarantees:
- Severity is graded, not measured (no depth/volume/size claims).
- Coordinates only appear if part of the location or map link text.
- Clean HTML and plain text alternatives for all email clients.
"""

from app.models import Authority, Pothole, ReportKind, SeverityBand


def get_subject_prefix(kind: ReportKind | str) -> str:
    """Return subject prefix based on report kind."""
    k = kind.value if isinstance(kind, ReportKind) else str(kind)
    if k == "reopened":
        return "[CivicLens] REOPENED: "
    elif k == "reminder":
        return "[CivicLens] REMINDER: "
    elif k == "escalation":
        return "[CivicLens] ESCALATION: "
    return "[CivicLens] "


def render_report_email(
    kind: ReportKind | str,
    pothole: Pothole,
    authority: Authority | None,
    crop_urls: list[str],
    map_url: str,
    fixed_url: str,
) -> tuple[str, str, str]:
    """Render (subject, text_content, html_content) for a report email.

    crop_urls: presigned image URLs (first is before crop, subsequent are after crops).
    """
    severity_str = (
        pothole.severity.value
        if isinstance(pothole.severity, SeverityBand)
        else str(pothole.severity or "low")
    )
    severity_display = severity_str.capitalize()

    city_name = authority.city if authority and authority.city else "Demo City"
    place_name = f"{city_name} road section"

    prefix = get_subject_prefix(kind)
    subject = f"{prefix}{severity_display} pothole near {city_name}"

    k = kind.value if isinstance(kind, ReportKind) else str(kind)

    # 1. Plain text body
    if k == "reopened":
        lead_text = (
            "A pothole previously claimed as fixed has been detected again by passes. "
            "Fresh visual evidence indicates the road defect remains open."
        )
    elif k == "reminder":
        lead_text = (
            "This is a reminder regarding an outstanding pothole report not yet marked fixed."
        )
    elif k == "escalation":
        lead_text = (
            "This report has been escalated due to an elapsed timeline without verification."
        )
    else:
        lead_text = (
            "A road surface pothole has been detected and verified by automated dashcam telemetry."
        )

    before_line = (
        f"Photo (Road surface crop): {crop_urls[0]}" if crop_urls else "No photo available"
    )
    after_lines = ""
    if len(crop_urls) > 1:
        after_lines = "\n" + "\n".join(
            f"After-repair evidence photo {idx + 1}: {url}" for idx, url in enumerate(crop_urls[1:])
        )

    text_body = f"""CivicLens Road Quality Report
Status: {k.capitalize()}

{lead_text}

Pothole ID: {pothole.id}
Severity: {severity_display}
Location: Near {place_name}

{before_line}{after_lines}

View Interactive Map & Verification Details:
{map_url}

When the road repair is completed, please confirm the repair using this Fixed link:
{fixed_url}

Note: A repair claim initiates drive-by verification by subsequent fleet passes.
This message was delivered automatically by CivicLens.
"""

    # 2. HTML body
    image_html = ""
    if crop_urls:
        image_html += f"""
        <div style="margin: 20px 0;">
            <p style="font-size: 13px; color: #555; margin-bottom: 6px;">Road evidence crop:</p>
            <img src="{crop_urls[0]}" alt="Pothole crop"
                 style="max-width: 100%; border-radius: 6px; border: 1px solid #ddd;" />
        </div>
        """
        if len(crop_urls) > 1:
            for idx, url in enumerate(crop_urls[1:]):
                image_html += f"""
                <div style="margin: 20px 0;">
                    <p style="font-size: 13px; color: #555; margin-bottom: 6px;">
                      Contradicting detection #{idx + 1}:
                    </p>
                    <img src="{url}" alt="Contradicting crop"
                         style="max-width: 100%; border-radius: 6px; border: 1px solid #ddd;" />
                </div>
                """

    if severity_str == "high":
        badge_color = "#dc2626"
    elif severity_str == "medium":
        badge_color = "#d97706"
    else:
        badge_color = "#2563eb"

    html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         line-height: 1.5; color: #1f2937; margin: 0; padding: 20px; }}
  .container {{ max-width: 580px; margin: 0 auto; background: #ffffff;
                border: 1px solid #e5e7eb; border-radius: 8px; padding: 24px; }}
  .badge {{ display: inline-block; padding: 4px 10px; border-radius: 4px;
            font-weight: 600; font-size: 12px; color: #ffffff; background-color: {badge_color}; }}
  .button {{ display: inline-block; background-color: #059669; color: #ffffff !important;
             padding: 12px 20px; text-decoration: none; border-radius: 6px;
             font-weight: 600; margin: 16px 0; }}
  .map-link {{ color: #2563eb; text-decoration: underline; }}
  .footer {{ margin-top: 24px; padding-top: 16px; border-top: 1px solid #e5e7eb;
             font-size: 12px; color: #6b7280; }}
</style>
</head>
<body>
<div class="container">
  <div style="display: flex; justify-content: space-between; align-items: center;
              margin-bottom: 16px;">
    <h2 style="margin: 0; font-size: 20px; color: #111827;">CivicLens Report</h2>
    <span class="badge">{severity_display} Severity</span>
  </div>
  <p>{lead_text}</p>
  <table style="width: 100%; border-collapse: collapse; margin: 16px 0;">
    <tr>
      <td style="padding: 6px 0; color: #4b5563; width: 120px;">Pothole ID:</td>
      <td style="padding: 6px 0; font-family: monospace;">{pothole.id}</td>
    </tr>
    <tr>
      <td style="padding: 6px 0; color: #4b5563;">Area:</td>
      <td style="padding: 6px 0;">Near {place_name}</td>
    </tr>
    <tr>
      <td style="padding: 6px 0; color: #4b5563;">Severity Grade:</td>
      <td style="padding: 6px 0; font-weight: 600;">{severity_display}</td>
    </tr>
  </table>

  {image_html}

  <p>
    <a href="{map_url}" class="map-link">View pothole location on the CivicLens Map</a>
  </p>

  <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px;
              padding: 16px; margin-top: 20px;">
    <h3 style="margin-top: 0; margin-bottom: 8px; font-size: 16px; color: #166534;">
      Road Repaired?
    </h3>
    <p style="margin-bottom: 12px; font-size: 14px; color: #14532d;">
      When work has concluded, claim the repair below to schedule automatic drive-by camera
      verification.
    </p>
    <a href="{fixed_url}" class="button">Claim as Fixed</a>
    <p style="margin: 0; font-size: 12px; color: #15803d;">
      Note: A claim is not final proof. It initiates camera verification on subsequent passes.
    </p>
  </div>

  <div class="footer">
    <p>This automated notification is part of the CivicLens Road Health Pilot. Report kind: {k}.</p>
  </div>
</div>
</body>
</html>
"""

    return subject, text_body, html_body
