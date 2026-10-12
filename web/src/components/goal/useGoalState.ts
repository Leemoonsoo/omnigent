import { useEffect, useState } from "react";
import { getGoal, type Goal } from "@/lib/goalApi";

interface UseGoalStateResult {
  goal: Goal | null;
  setGoal: (goal: Goal | null) => void;
}

/**
 * Keep the current goal snapshot for a composer session.
 *
 * The hook intentionally fails closed to ``null`` on read errors: the dialog
 * performs its own read and surfaces the error text when the user opens it.
 *
 * @param conversationId - Session whose goal to read, or ``null``.
 * @param enabled - ``false`` clears the snapshot without reading.
 * @param turnActive - Whether a turn is running. Each change re-reads the
 *   goal: a typed ``/goal`` sets it when its turn starts, and the harness can
 *   complete or limit it during a turn. The turn-start read can finish before
 *   the harness stores a typed goal, so the goal may only appear at turn end.
 * @returns The goal snapshot and a setter for dialog-driven updates.
 */
export function useGoalState(
  conversationId: string | null,
  enabled: boolean,
  turnActive = false,
): UseGoalStateResult {
  const [goal, setGoal] = useState<Goal | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!enabled || !conversationId) {
      setGoal(null);
      return;
    }
    void getGoal(conversationId)
      .then((response) => {
        if (!cancelled) setGoal(response.goal);
      })
      .catch(() => {
        if (!cancelled) setGoal(null);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, enabled, turnActive]);

  return { goal, setGoal };
}
