// GENERATED from conf/ui_theme.json — do not hand-edit.
// Regenerate: uv run python -m app.mcp.ui --write-app
export const theme = {
  "id": "grp",
  "version": "v0",
  "palette": {
    "base-100": "#fbfcfd",
    "base-200": "#f1f4f7",
    "base-300": "#e0e6ec",
    "base-content": "#16202b",
    "primary": "#0f6a8b",
    "primary-content": "#f6fbfc",
    "secondary": "#4a5b6b",
    "secondary-content": "#f6f8fa",
    "accent": "#7c5cbf",
    "accent-content": "#f9f7fd",
    "neutral": "#2b3641",
    "neutral-content": "#eef2f5",
    "info": "#2a7ab0",
    "success": "#1f7a4d",
    "warning": "#a06a08",
    "error": "#9b2c2c"
  },
  "provenance": {
    "section_edges": [
      "#38bdf8",
      "#34d399",
      "#fbbf24",
      "#a78bfa"
    ],
    "parse_edge": "#a78bfa",
    "cite_edge": "#94a3b8"
  },
  "validationLevels": {
    "multi-agency-consensus": "verified",
    "peer-reviewed": "verified",
    "single-agency": "neutral",
    "model-output": "caution"
  },
  "trustRules": {
    "success_reserved_for": "server_verified",
    "default_state": "unverified",
    "chip_states": [
      "pass",
      "fail",
      "not_run"
    ],
    "verified_requires_receipt_link": true,
    "never": [
      "a verdict may not be set by the host/client \u2014 it resolves from the server",
      "declines, declared gaps, honest zeros and ADJUSTED labels may not be suppressed",
      "no partner/agency logo packs; only a neutral built-with mark carrying a receipt link"
    ]
  },
  "voice": {
    "decline": "The system declined to answer \u2014 declining beats guessing.",
    "adjusted": "ADJUSTED by the requester (not the hub default)",
    "hub_default": "hub default configuration",
    "honest_zero": "0 matched \u2014 declared as a gap, not hidden",
    "unverified": "unverified by this platform",
    "claim_scope": "verified = every cited claim traces to the evidence pack; NOT the truth of the underlying sources",
    "retrieved_uncited": "retrieved, not cited by any claim"
  }
} as const;
