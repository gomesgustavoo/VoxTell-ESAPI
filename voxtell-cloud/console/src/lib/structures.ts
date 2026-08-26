// Free-text prompt -> the TPS structure colour it should be drawn in.
//
// This is what makes the ledger the SAME device on both surfaces: the landing page
// colours its contours from --vx-s-* tokens supplied by the generator, and here the
// dashboard colours a job's prompt chips from the same set by matching the words a
// planner typed. A prompt is free text, so this is a best-effort lookup — anything
// unmatched falls back to a neutral border rather than guessing.
//
// The tokens live in design/tokens.css and are generated into tokens.generated.css's
// plain :root block as --vx-s-*, NOT into Tailwind's @theme. Tailwind v4 prunes any
// @theme colour no utility references, and these are picked at runtime from a prompt's
// text — so in @theme, 22 of 23 were dropped and every mark stroked `none`. The name
// matches what the landing page's hero uses, so the ledger really is one device.

interface Rule {
  token: string;
  match: string[];
}

// Ordered: the first match wins, so put the specific before the general.
// "portal vein" must beat "vein", and "vena cava" must beat "vein".
const RULES: Rule[] = [
  { token: "s-liver", match: ["liver", "hepat"] },
  { token: "s-spleen", match: ["spleen", "splenic art", "lien"] },
  { token: "s-stomach", match: ["stomach", "gastric"] },
  { token: "s-pancreas", match: ["pancrea"] },
  { token: "s-ivc", match: ["vena cava", "ivc", "cava"] },
  { token: "s-vein", match: ["portal", "splenic vein", "vein", "venous"] },
  { token: "s-aorta", match: ["aorta", "aortic"] },
  { token: "s-cord", match: ["spinal cord", "cord", "myelum", "medulla spinalis"] },
  { token: "s-vertebra", match: ["vertebra", "vertebral", "spine", "spinal canal"] },
  { token: "s-bone", match: ["rib", "costa", "sternum", "clavic", "scapula", "femur", "pelvi", "sacrum", "humerus", "skull"] },
  { token: "s-cartilage", match: ["cartilage"] },
  { token: "s-colon", match: ["colon", "bowel", "rectosigmoid", "caecum", "cecum", "duoden", "intestine"] },
  { token: "s-muscle", match: ["muscle", "erector spinae", "autochthon", "paraspinal", "psoas", "gluteus"] },
  { token: "s-adrenal", match: ["adrenal", "suprarenal"] },
  { token: "s-kidney", match: ["kidney", "renal", "nephr"] },
  { token: "s-heart", match: ["heart", "cardiac", "myocard", "atrium", "ventricle"] },
  { token: "s-lung", match: ["lung", "pulmon", "bronch", "trachea"] },
  { token: "s-bladder", match: ["bladder", "vesic"] },
  { token: "s-esophagus", match: ["esophag", "oesophag", "gullet"] },
  { token: "s-rectum", match: ["rectum", "rectal", "anal"] },
  { token: "s-brain", match: ["brain", "cerebr", "cerebell", "hippocamp", "thalam", "pituitar"] },
  { token: "s-parotid", match: ["parotid", "submandib", "salivary"] },
  { token: "s-target", match: ["gtv", "ctv", "ptv", "tumour", "tumor", "lesion", "metasta", "nodule", "neoplas"] },
];

/** The --vx-s-* token for a prompt, or null when nothing matches. */
export function structureToken(prompt: string): string | null {
  const q = prompt.toLowerCase();
  for (const rule of RULES) {
    if (rule.match.some((m) => q.includes(m))) return rule.token;
  }
  return null;
}

/** An inline style so the colour can come from a CSS variable chosen at runtime. */
export function structureColour(prompt: string): string | undefined {
  const token = structureToken(prompt);
  return token ? `var(--vx-${token})` : undefined;
}
