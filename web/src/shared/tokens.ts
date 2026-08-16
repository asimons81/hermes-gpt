/**
 * Design tokens — TypeScript mirror of tokens.css (neutral defaults).
 *
 * Single source of truth for values the JS layer needs (colors, spacing,
 * radii, z-index). Keep in sync with tokens.css; the media lane
 * (t_d2d86b5a) owns final values and updates both files together.
 */

export const tokens = {
  color: {
    bg: "#ffffff",
    bgSubtle: "#f6f7f8",
    bgRaised: "#ffffff",
    surface: "#f0f1f3",
    surfaceHover: "#e6e8eb",
    border: "#d5d8dc",
    borderStrong: "#b8bdc4",
    text: "#1a1d21",
    textSecondary: "#5c636a",
    textMuted: "#8a919a",
    textOnAccent: "#ffffff",
    accent: "#2f6fed",
    accentHover: "#2559c9",
    accentSubtle: "#e8effd",
    success: "#1a7f37",
    successSubtle: "#e6f4ea",
    warning: "#9a6700",
    warningSubtle: "#fff8c5",
    danger: "#cf222e",
    dangerSubtle: "#ffebe9",
    neutralSubtle: "#f0f1f3",
  },
  space: { 1: 4, 2: 8, 3: 12, 4: 16, 5: 24, 6: 32, 8: 48 },
  radius: { sm: 6, md: 10, lg: 14, pill: 999 },
  fontSize: {
    xs: 12,
    sm: 13,
    md: 15,
    lg: 17,
    xl: 20,
    xxl: 26,
  },
  z: { base: 0, sticky: 100, overlay: 200, modal: 300, toast: 400 },
  duration: { fast: 120, normal: 200 },
  layout: { sidebarWidth: 280, headerHeight: 56, contentMaxWidth: 880 },
} as const;

export type Tokens = typeof tokens;
