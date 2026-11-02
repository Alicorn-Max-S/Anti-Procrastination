// Prep doc theme: every color token from the class's subject color + light/dark mode.
// A JS port of the design handoff's theme.ts (frozen Oct 31 2026): the page has no build step, so
// only the TypeScript types are dropped. Don't change the math here without re-checking contrast:
// every text token is pushed until it passes WCAG AA (4.5:1).
// Nov 1 redesign: the page draws each class in a muted version of its color (`muted`), so the
// classes sit together on a black-and-white page. The server still stores `hex`.
// Nov 2 warmstart brand (docs/brand.md): Clay is warmstart's own color (the shell, the start-time
// check-in, the block that's on now) and the classes only label their own class and prep doc. So
// the muted colors are quieter (OKLCH chroma about 0.05), red and orange are moved off Clay's hue,
// and the neutrals are zinc. deriveTheme(BRAND.accent) gives the Clay version of every token.

const SUBJECT_COLORS = [
  { id: "blue", name: "Blue", hex: "#3D5AFE", muted: "#677C99" }, // default
  { id: "purple", name: "Purple", hex: "#7C3AED", muted: "#87799A" },
  { id: "pink", name: "Pink", hex: "#D6409F", muted: "#A1798A" },
  { id: "red", name: "Red", hex: "#E5484D", muted: "#936567" },
  { id: "orange", name: "Orange", hex: "#F0812A", muted: "#95745C" },
  { id: "yellow", name: "Yellow", hex: "#D9A21B", muted: "#928A63" },
  { id: "green", name: "Green", hex: "#2E9E5B", muted: "#6D8B72" },
  { id: "teal", name: "Teal", hex: "#0E9AA7", muted: "#5B8086" },
];

/** warmstart's Clay palette (style.css has the same values as --ws-* tokens). */
const BRAND = { accent: "#B9624A", stop2: "#C97E45", tint: "#F4E6E2", light: "#CE9180", deep: "#55342D" };

const DEFAULT_SUBJECT = "#3D5AFE";

/** The muted color the page draws for a stored subject color (a color off the palette is kept). */
function mutedSubject(hex) {
  const h = /^#[0-9a-fA-F]{6}$/.test(hex || "") ? hex.toUpperCase() : DEFAULT_SUBJECT;
  return SUBJECT_COLORS.find((c) => c.hex === h)?.muted || h;
}

/** Watch-out is always this red, whatever the subject color. */
const WATCH_OUT_RED = "#E0362C";

// Zinc greys (no blue tint). Dark mode sits on the brand's dark surface, #18181B.
const NEUTRALS = {
  light: { page: "#FFFFFF", line: "#E4E4E7", tile: "#F4F4F5", fill: "#E4E4E7", text: "#18181B", text2: "#3F3F46", muted: "#52525B", field: "#FFFFFF", ctrl: "#84848D" },
  dark: { page: "#18181B", line: "#52525B", tile: "#27272A", fill: "#3F3F46", text: "#F4F4F5", text2: "#D4D4D8", muted: "#A1A1AA", field: "#111113", ctrl: "#71717A" },
};

// ---------- color math ----------
const hexToRgb = (h) => {
  const s = h.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(s.slice(i, i + 2), 16));
};
const rgbToHex = (c) =>
  "#" + c.map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("").toUpperCase();

/** Linear RGB blend: w = weight of `a` (0..1). mix(c, '#FFFFFF', 0.22) = 22% c, 78% white. */
const mix = (a, b, w) => {
  const A = hexToRgb(a), B = hexToRgb(b);
  return rgbToHex(A.map((v, i) => v * w + B[i] * (1 - w)));
};

const luminance = (h) => {
  const c = hexToRgb(h).map((v) => {
    const x = v / 255;
    return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};

const contrast = (a, b) => {
  const x = luminance(a), y = luminance(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
};

/** Push `color` toward `toward` in 5% steps until it reaches `min` contrast on `bg`. */
const ensure = (color, bg, min, toward) => {
  let out = color;
  for (let i = 1; i <= 20 && contrast(out, bg) < min; i++) out = mix(toward, color, i * 0.05);
  return out;
};

// ---------- theme ----------
function deriveTheme(subjectHex, mode) {
  const dark = mode === "dark";
  const n = NEUTRALS[mode];
  const subject = mutedSubject(subjectHex);
  const brand = subject === BRAND.accent;

  // Soft tint: a light wash of the class (18%), or the brand's own tint (16% Clay). Dark mode
  // mixes into the page, which is a dark grey and not black.
  const soft = dark ? mix(subject, n.page, 0.2) : brand ? BRAND.tint : mix(subject, "#FFFFFF", 0.18);

  // "Soft tone": the subject pulled toward the page, then pushed back until readable. Clay starts
  // from itself (Clay light in dark mode) and darkens toward its own deep tone, not toward the
  // ink, so its text and Start buttons stay clay instead of going grey-brown.
  const mid = brand ? (dark ? BRAND.light : BRAND.accent) : dark ? mix(subject, n.page, 0.5) : mix(subject, "#FFFFFF", 0.55);
  const toward = brand && !dark ? BRAND.deep : n.text;
  const softText = ensure(mid, soft, 4.5, toward);
  // Dots and bars. The muted colors are already mid-tone, so light mode draws them as they are.
  const graphic = dark ? (brand ? BRAND.light : mix(subject, "#FFFFFF", 0.45)) : subject;
  const softRing = dark ? mix(softText, soft, 0.55) : "transparent";

  let accentText = ensure(mid, n.page, 4.5, toward);
  accentText = ensure(accentText, n.tile, 4.5, toward);
  accentText = ensure(accentText, soft, 4.5, toward);

  const warnBg = dark ? mix(WATCH_OUT_RED, n.page, 0.32) : mix(WATCH_OUT_RED, "#FFFFFF", 0.34);
  // Dark mode lifts the red toward a light red, not toward white, so it stays a clear red and can't
  // be mistaken for Clay light (#CE9180) next to it.
  const warnText = ensure(WATCH_OUT_RED, warnBg, 4.5, dark ? "#FF8A80" : n.text);

  return {
    ...n,
    subject,
    soft,
    softText,
    softRing,
    accentText,
    graphic,
    inputBorder: ensure(graphic, n.field, 3, n.text),
    heroButtonBg: dark ? mix(softText, soft, 0.25) : n.field,
    heroButtonText: dark ? n.text : softText,
    heroOutline: softText,
    heroTrack: dark ? "rgba(255,255,255,0.14)" : "rgba(0,0,0,0.08)",
    heroFill: dark ? graphic : softText,
    warnBg,
    warnText,
  };
}

/** CSS custom properties for a prep doc root element, e.g. <div style={themeToCssVars(t)}>. */
function themeToCssVars(t) {
  const kebab = (k) => k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
  return Object.fromEntries(Object.entries(t).map(([k, v]) => [`--pd-${kebab(k)}`, v]));
}
