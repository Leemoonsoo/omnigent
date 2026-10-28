import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import { HistoryNavigationButtons } from "./HistoryNavigationButtons";

interface ShellWindow {
  omnigentDesktop?: unknown;
  omnigentNative?: unknown;
  navigation?: unknown;
}

/** Stand-in for the Navigation API: jsdom does not implement it. */
class FakeNavigation extends EventTarget {
  canGoBack = false;
  canGoForward = false;

  move(state: { canGoBack: boolean; canGoForward: boolean }) {
    this.canGoBack = state.canGoBack;
    this.canGoForward = state.canGoForward;
    this.dispatchEvent(new Event("currententrychange"));
  }
}

function installNavigation(state: { canGoBack: boolean; canGoForward: boolean }) {
  const navigation = new FakeNavigation();
  navigation.canGoBack = state.canGoBack;
  navigation.canGoForward = state.canGoForward;
  Object.defineProperty(window, "navigation", { configurable: true, value: navigation });
  return navigation;
}

/** A phone shell bridge with the in-sidebar server picker. */
function phoneShell(kind: "ios" | "android") {
  return {
    kind,
    getServerPicker: () => Promise.resolve(null),
    switchServer: () => Promise.resolve(),
    openServerSetup: () => {},
  };
}

function CurrentPath() {
  return <output aria-label="Current path">{useLocation().pathname}</output>;
}

function renderButtons(entries = ["/"], index = entries.length - 1) {
  return render(
    <MemoryRouter initialEntries={entries} initialIndex={index}>
      <TooltipProvider>
        <HistoryNavigationButtons />
        <CurrentPath />
      </TooltipProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  const shellWindow = window as ShellWindow;
  delete shellWindow.omnigentDesktop;
  delete shellWindow.omnigentNative;
  delete shellWindow.navigation;
});

describe("HistoryNavigationButtons", () => {
  it("renders nothing in a browser tab, which has its own Back/Forward", () => {
    installNavigation({ canGoBack: true, canGoForward: true });
    renderButtons();
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Forward" })).toBeNull();
  });

  it("renders nothing in a shell whose WebView lacks the Navigation API", () => {
    (window as ShellWindow).omnigentNative = phoneShell("ios");
    renderButtons();
    expect(screen.queryByTestId("history-navigation")).toBeNull();
  });

  it.each(["ios", "android"] as const)(
    "renders nothing in an older %s shell whose server switcher floats over the header",
    (kind) => {
      (window as ShellWindow).omnigentNative = { kind };
      installNavigation({ canGoBack: true, canGoForward: true });
      renderButtons();
      expect(screen.queryByTestId("history-navigation")).toBeNull();
    },
  );

  it.each([
    ["Electron", { omnigentDesktop: { kind: "electron" } }],
    ["iOS", { omnigentNative: phoneShell("ios") }],
    ["Android", { omnigentNative: phoneShell("android") }],
  ])("shows both buttons in the %s shell", (_shell, bridge) => {
    Object.assign(window, bridge);
    installNavigation({ canGoBack: true, canGoForward: false });
    renderButtons();
    expect(screen.getByRole("button", { name: "Back" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Forward" })).toBeDisabled();
  });

  it("follows history changes and steps through the router", () => {
    (window as ShellWindow).omnigentNative = phoneShell("android");
    const navigation = installNavigation({ canGoBack: true, canGoForward: false });
    renderButtons(["/c/first", "/c/second"]);

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByRole("status", { name: "Current path" })).toHaveTextContent("/c/first");

    act(() => navigation.move({ canGoBack: false, canGoForward: true }));
    expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Forward" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Forward" }));
    expect(screen.getByRole("status", { name: "Current path" })).toHaveTextContent("/c/second");
  });
});
