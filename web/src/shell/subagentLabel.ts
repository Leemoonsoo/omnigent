// Display label for a child (sub-agent) session, shared by the Agents rail
// rows and the approval cards a sub-agent's prompts mirror into, so a card
// names the sub-agent exactly as the rail lists it.

import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { CLAUDE_NATIVE_SUBAGENT_WRAPPER, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";

export const CODEX_NATIVE_SUBAGENT_WRAPPER = "codex-native-ui-subagent";
export const OPENCODE_NATIVE_SUBAGENT_WRAPPER = "opencode-native-ui-subagent";
export const ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER = "antigravity-native-ui-subagent";

/**
 * Pick the primary label for a child-session row.
 *
 * @param child - One child-session summary from the poll or stream.
 * @returns The label shown beside the child icon.
 */
export function childPrimaryLabel(child: ChildSessionInfo): string {
  // User-added rows use the reserved "ui:<agent>:<name>" title sentinel;
  // LLM-spawned titles cannot start with "ui:" because the spec validator
  // rejects "ui" as a sub-agent name.
  const isUserAdded = child.title?.startsWith("ui:") ?? false;
  const childWrapper = child.labels?.[WRAPPER_LABEL_KEY];
  // agy joins these rather than taking the generic path below: its child title
  // is ``"<role>:<cascade id>"``, so the first-colon split puts the ROLE in
  // ``tool`` and the cascade UUID in the suffix — and the generic path returns
  // ``session_name ?? suffix``, both of which are that UUID.
  const isNativeSubagent =
    childWrapper === CODEX_NATIVE_SUBAGENT_WRAPPER ||
    childWrapper === OPENCODE_NATIVE_SUBAGENT_WRAPPER ||
    childWrapper === ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER ||
    childWrapper === CLAUDE_NATIVE_SUBAGENT_WRAPPER;
  if (isNativeSubagent && !isUserAdded) {
    return child.tool ?? child.title ?? child.id;
  }
  let titleTask: string | null = null;
  if (child.title?.includes(":")) {
    const titleSuffix = child.title.split(":").slice(1).join(":");
    if (titleSuffix) titleTask = titleSuffix;
  }
  return (
    child.task_summary ?? child.session_name ?? titleTask ?? child.title ?? child.tool ?? child.id
  );
}
