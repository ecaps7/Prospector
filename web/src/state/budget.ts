import type { EffortLevel } from "../api/types";

export type StageBudget = {
  maxConcurrency: number;
  maxWorkerRounds: number;
};

export type ResearchLimits = {
  decisionRoundLimit: number;
  stages: Record<string, StageBudget>;
};

const EFFORT_LIMITS: Record<EffortLevel, ResearchLimits> = {
  quick: {
    decisionRoundLimit: 8,
    stages: {
      scout: { maxConcurrency: 6, maxWorkerRounds: 16 },
      deep_dive: { maxConcurrency: 3, maxWorkerRounds: 32 },
      verify: { maxConcurrency: 3, maxWorkerRounds: 12 },
    },
  },
  standard: {
    decisionRoundLimit: 12,
    stages: {
      scout: { maxConcurrency: 6, maxWorkerRounds: 24 },
      deep_dive: { maxConcurrency: 3, maxWorkerRounds: 64 },
      verify: { maxConcurrency: 3, maxWorkerRounds: 18 },
    },
  },
  deep: {
    decisionRoundLimit: 24,
    stages: {
      scout: { maxConcurrency: 8, maxWorkerRounds: 32 },
      deep_dive: { maxConcurrency: 5, maxWorkerRounds: 81 },
      verify: { maxConcurrency: 5, maxWorkerRounds: 24 },
    },
  },
};

export function limitsForEffort(effort: string): ResearchLimits {
  return EFFORT_LIMITS[(effort as EffortLevel) in EFFORT_LIMITS ? (effort as EffortLevel) : "standard"];
}

export function maxConcurrency(effort: string, stage: string | undefined): number {
  const limits = limitsForEffort(effort);
  if (stage && limits.stages[stage]) return limits.stages[stage].maxConcurrency;
  return Math.max(...Object.values(limits.stages).map((item) => item.maxConcurrency));
}
