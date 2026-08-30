import type { EffortLevel } from "../api/types";

export type ResearchLimits = {
  decisionRoundLimit: number;
  maxConcurrency: number;
  maxWorkerRounds: number;
};

/** 手抄自 `deterministic/budget.py` 的 EFFORT_LIMITS。改后端记得改这里。 */
const EFFORT_LIMITS: Record<EffortLevel, ResearchLimits> = {
  quick: {
    decisionRoundLimit: 8,
    maxConcurrency: 6,
    maxWorkerRounds: 12,
  },
  standard: {
    decisionRoundLimit: 12,
    maxConcurrency: 5,
    maxWorkerRounds: 20,
  },
  deep: {
    decisionRoundLimit: 24,
    maxConcurrency: 6,
    maxWorkerRounds: 32,
  },
};

export function limitsForEffort(effort: string): ResearchLimits {
  return EFFORT_LIMITS[(effort as EffortLevel) in EFFORT_LIMITS ? (effort as EffortLevel) : "standard"];
}

export function maxConcurrency(effort: string): number {
  return limitsForEffort(effort).maxConcurrency;
}
