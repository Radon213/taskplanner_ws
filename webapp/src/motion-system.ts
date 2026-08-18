/**
 * Taskplanner motion language
 *
 * One composed "Silk" personality is shared across the product. Motion is
 * reserved for spatial continuity, state changes, and feedback; clinical data
 * itself is never delayed or counted into view.
 */

export const SILK_EASE = [0.32, 0.72, 0, 1] as const;
export const SILK_EXIT_EASE = [0.4, 0, 1, 1] as const;

export const MOTION_DURATION = {
  instant: 0.08,
  fast: 0.12,
  normal: 0.2,
  moderate: 0.3,
  entrance: 0.45,
} as const;

/** Exact StyleSeed Silk seed recipes. */
export const silk = {
  entrance: {
    initial: { opacity: 0, y: 12 },
    animate: {
      opacity: 1,
      y: 0,
      transition: { ease: SILK_EASE, duration: MOTION_DURATION.entrance },
    },
  },
  exit: {
    exit: {
      opacity: 0,
      y: -6,
      transition: { ease: SILK_EXIT_EASE, duration: MOTION_DURATION.moderate },
    },
  },
  hover: {
    whileHover: {
      y: -1,
      filter: "brightness(1.03)",
      transition: { ease: SILK_EASE, duration: 0.25 },
    },
  },
  press: {
    whileTap: {
      filter: "brightness(0.96)",
      transition: { ease: [0.4, 0, 0.2, 1] as const, duration: 0.15 },
    },
  },
  layout: {
    layout: true as const,
    transition: { ease: SILK_EASE, duration: 0.4 },
  },
} as const;

/** Exact StyleSeed `stagger-cascade` choreography for short, non-critical lists. */
export const staggerCascade = {
  container: {
    hidden: {},
    show: { transition: { staggerChildren: 0.07 } },
  },
  item: {
    hidden: { opacity: 0, y: 12 },
    show: { opacity: 1, y: 0 },
  },
} as const;

/** Exact StyleSeed `shimmer` timing. The gradient itself is tokenized in CSS. */
export const shimmer = {
  animate: { backgroundPosition: ["200% 0", "-200% 0"] as string[] },
  transition: { duration: 1.5, repeat: Infinity, ease: "linear" as const },
} as const;

export const quietFade = {
  initial: { opacity: 0 },
  animate: { opacity: 1 },
  exit: { opacity: 0 },
  transition: { duration: MOTION_DURATION.normal, ease: SILK_EASE },
} as const;

export const statusSwap = {
  initial: { opacity: 0, y: 6 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -4 },
  transition: { duration: MOTION_DURATION.normal, ease: SILK_EASE },
} as const;
