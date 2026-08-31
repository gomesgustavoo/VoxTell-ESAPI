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

// --------------------------------------------------------------------------- //
// Describing a job's targets, however it was addressed
// --------------------------------------------------------------------------- //
// A job names EITHER free-text prompts or catalog structure ids. Every view that
// used to read `job.prompts` directly would render "—" and "No prompts recorded"
// for a perfectly good CADS job, which reads as a broken job rather than a
// differently-addressed one. So all of them go through here instead.

import type { Job } from "./api";

/** Human labels for what a job asked for, whichever way it was addressed. */
export function jobTargets(job: Job): string[] {
  if (job.prompts && job.prompts.length > 0) return job.prompts;

  // "cads_556.rectum" -> "rectum". The model is shown separately, so repeating it
  // on every chip is noise; the full id stays available as a title attribute.
  return (job.structure_ids ?? []).map((id) => {
    const dot = id.indexOf(".");
    const tail = dot >= 0 ? id.slice(dot + 1) : id;
    return tail.replace(/_/g, " ");
  });
}

/** "prompt" or "structure", singular/plural, for a count label. */
export function jobTargetNoun(job: Job, count: number): string {
  const noun = job.prompts && job.prompts.length > 0 ? "prompt" : "structure";
  return count === 1 ? noun : `${noun}s`;
}

/** The models a job ran, or an empty list for an older job that did not record them. */
export function jobModels(job: Job): string[] {
  return job.models ?? [];
}
