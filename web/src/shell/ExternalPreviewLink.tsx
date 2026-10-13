import type { ComponentPropsWithoutRef } from "react";
import { followLinkWithPopupFallback } from "@/lib/openLinkInApp";
import { useFileViewerConversationId } from "./FileViewerContext";

/**
 * Anchor for a URL (`https:`, `mailto:`, `//host`) in a read-only file preview.
 * It opens the way a chat link does: a new tab on the web, and in the desktop
 * app the shell's window-open policy (the system browser, or the in-app browser
 * when opted in). A same-window link would instead navigate the app itself off
 * its server, which a Databricks browser sign-in window treats as a lost
 * session and drops back to the connect screen.
 */
export function ExternalPreviewLink({
  href,
  children,
  onClick,
  node: _node,
  ...props
}: ComponentPropsWithoutRef<"a"> & { href: string; node?: unknown }) {
  const conversationId = useFileViewerConversationId();
  return (
    <a
      {...props}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => {
        onClick?.(event);
        followLinkWithPopupFallback(event, href, conversationId);
      }}
    >
      {children}
    </a>
  );
}
