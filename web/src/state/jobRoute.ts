import { useOutletContext } from "react-router-dom";
import type { JobDetail } from "../api/types";

/**
 * 任务快照由 App 拉取一次，任务栏和报告页读同一份：两个地方各拉各的，迟早会
 * 在同一屏上显示两种状态。
 */
export type JobRouteContext = {
  /** 当前路由这个任务的快照；还没拿到（或拿失败）时为 null。 */
  job: JobDetail | null;
  /** 快照拉取失败时的那一句；`job` 非空说明手上还有一份旧的可用。 */
  jobError: string | null;
};

export function useJobRoute(): JobRouteContext {
  return useOutletContext<JobRouteContext>();
}
