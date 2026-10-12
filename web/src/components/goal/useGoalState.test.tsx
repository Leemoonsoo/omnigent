import type * as GoalApiModule from "@/lib/goalApi";

import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getGoal, type Goal } from "@/lib/goalApi";
import { useGoalState } from "./useGoalState";

vi.mock("@/lib/goalApi", async (importOriginal) => {
  const actual = await importOriginal<typeof GoalApiModule>();
  return { ...actual, getGoal: vi.fn() };
});

const mockGetGoal = vi.mocked(getGoal);

const GOAL: Goal = {
  objective: "Ship goal mode",
  status: "active",
  tokenBudget: 40000,
  tokensUsed: 1200,
  timeUsedSeconds: 125,
  createdAt: null,
  updatedAt: null,
};

beforeEach(() => {
  mockGetGoal.mockReset();
});

describe("useGoalState", () => {
  it("loads the goal when enabled for a conversation", async () => {
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL });

    const { result } = renderHook(() => useGoalState("conv", true));

    await waitFor(() => expect(result.current.goal).toEqual(GOAL));
    expect(mockGetGoal).toHaveBeenCalledWith("conv");
  });

  it("clears state when disabled and fails closed on errors", async () => {
    mockGetGoal.mockRejectedValueOnce(new Error("offline"));

    const { result, rerender } = renderHook(({ enabled }) => useGoalState("conv", enabled), {
      initialProps: { enabled: true },
    });
    result.current.setGoal(GOAL);
    await waitFor(() => expect(result.current.goal).toBeNull());

    rerender({ enabled: false });
    expect(result.current.goal).toBeNull();
  });

  it("reloads the goal when a turn starts and ends", async () => {
    // A typed /goal sets the goal on the runner, outside this hook's writes.
    mockGetGoal
      .mockResolvedValueOnce({ goal: null })
      .mockResolvedValueOnce({ goal: GOAL })
      .mockResolvedValueOnce({ goal: { ...GOAL, status: "complete" } });

    const { result, rerender } = renderHook(
      ({ turnActive }) => useGoalState("conv", true, turnActive),
      { initialProps: { turnActive: false } },
    );
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(1));
    expect(result.current.goal).toBeNull();

    rerender({ turnActive: true });
    await waitFor(() => expect(result.current.goal).toEqual(GOAL));

    rerender({ turnActive: false });
    await waitFor(() => expect(result.current.goal?.status).toBe("complete"));
    expect(mockGetGoal).toHaveBeenCalledTimes(3);
  });

  it("shows a typed goal at turn end when the turn-start read was too early", async () => {
    // The turn-start read can reach the runner before it stores the goal.
    mockGetGoal
      .mockResolvedValueOnce({ goal: null })
      .mockResolvedValueOnce({ goal: null })
      .mockResolvedValueOnce({ goal: GOAL });

    const { result, rerender } = renderHook(
      ({ turnActive }) => useGoalState("conv", true, turnActive),
      { initialProps: { turnActive: false } },
    );
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(1));

    rerender({ turnActive: true });
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(2));
    expect(result.current.goal).toBeNull();

    rerender({ turnActive: false });
    await waitFor(() => expect(result.current.goal).toEqual(GOAL));
  });

  it("reloads the goal when a session becomes reachable again", async () => {
    mockGetGoal.mockResolvedValueOnce({ goal: GOAL });

    const { result, rerender } = renderHook(({ reachable }) => useGoalState("conv", reachable), {
      initialProps: { reachable: false },
    });
    expect(mockGetGoal).not.toHaveBeenCalled();

    rerender({ reachable: true });

    await waitFor(() => expect(result.current.goal).toEqual(GOAL));
    expect(mockGetGoal).toHaveBeenCalledTimes(1);
    expect(mockGetGoal).toHaveBeenCalledWith("conv");
  });
});
