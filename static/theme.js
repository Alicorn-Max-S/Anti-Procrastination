// Prep doc theme: every color token from the class's subject color + light/dark mode.
// A line-for-line JS port of the design handoff's theme.ts (frozen Oct 31 2026): the page has no
// build step, so only the TypeScript types are dropped. Don't change the math here without
// re-checking contrast: every text token is pushed until it passes WCAG AA (4.5:1).

const SUBJECT_COLORS = [
  { id: "blue", name: "Blue", hex: "#3D5AFE" }, // default / app color
  { id: "purple", name: "Purple", hex: "#7C3AED" },
  { id: "pink", name: "Pink", hex: "#D6409F" },
  { id: "red", name: "Red", hex: "#E5484D" },
  { id: "orange", name: "Orange", hex: "#F0812A" },
  { id: "yellow", name: "Yellow", hex: "#D9A21B" },
  { id: "green", name: "Green", hex: "#2E9E5B" },
  { id: "teal", name: "Teal", hex: "#0E9AA7" },
];

const DEFAULT_SUBJECT = "#3D5AFE";

/** Watch-out is always this red, whatever the subject color. */
const WATCH_OUT_RED = "#E0362C";

const NEUTRALS = {
  light: { page: "#FFFFFF", line: "#E4E4E1", tile: "#F4F4F2", fill: "#E8E8E5", text: "#141414", text2: "#474747", muted: "#5F5F5F", field: "#FFFFFF", ctrl: "#8A8A8F" },
  dark: { page: "#0A0A0A", line: "#5A5A5A", tile: "#161616", fill: "#2A2A2A", text: "#F2F2F0", text2: "#BDBDBD", muted: "#9A9A9A", field: "#0F0F0F", ctrl: "#76767C" },
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
  const subject = /^#[0-9a-fA-F]{6}$/.test(subjectHex) ? subjectHex.toUpperCase() : DEFAULT_SUBJECT;
  const isDefault = subject === DEFAULT_SUBJECT;

  // Soft tint. Blue uses hand-picked values; every other subject is mixed.
  const soft = dark
    ? (isDefault ? "#07102D" : mix(subject, "#000000", 0.17))
    : (isDefault ? "#D2DCFF" : mix(subject, "#FFFFFF", 0.22));

  // "Soft tone": the subject pulled toward the page, then pushed back until readable.
  const mid = dark ? mix(subject, "#000000", 0.5) : mix(subject, "#FFFFFF", 0.55);
  const softText = ensure(mid, soft, 4.5, n.text);
  const graphic = dark ? mix(subject, "#FFFFFF", 0.45) : mid;
  const softRing = dark ? mix(softText, soft, 0.55) : "transparent";

  let accentText = ensure(mid, n.page, 4.5, n.text);
  accentText = ensure(accentText, n.tile, 4.5, n.text);
  accentText = ensure(accentText, soft, 4.5, n.text);

  const warnBg = dark ? mix(WATCH_OUT_RED, "#000000", 0.32) : mix(WATCH_OUT_RED, "#FFFFFF", 0.34);
  const warnText = ensure(WATCH_OUT_RED, warnBg, 4.5, n.text);

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
