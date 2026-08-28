import type { EffortLevel } from "../api/types";

export type ResearchLimits = {
  decisionRoundLimit: number;
  maxConcurrency: number;
  maxWorkerRounds: number;
};

const EFFORT_LIMITS: Record<EffortLevel, ResearchLimits> = {
  quick: {
    decisionRoundLimit: 8,
    maxConcurrency: 6,
    maxWorkerRounds: 24,
  },
  standard: {
    decisionRoundLimit: 12,
    maxConcurrency: 5,
    maxWorkerRounds: 48,
  },
  deep: {
    decisionRoundLimit: 24,
    maxConcurrency: 6,
    maxWorkerRounds: 72,
  },
};

export function limitsForEffort(effort: string): ResearchLimits {
  return EFFORT_LIMITS[(effort as EffortLevel) in EFFORT_LIMITS ? (effort as EffortLevel) : "standard"];
}

export function maxConcurrency(effort: string): number {
  return limitsForEffort(effort).maxConcurrency;
}
