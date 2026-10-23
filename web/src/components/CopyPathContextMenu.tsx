// Right-click (long-press on touch) menu that copies a file path. Chat file
// links carry no href, so neither the browser nor the desktop shell offers to
// copy their target.

import type React from "react";
import { CopyIcon } from "lucide-react";
import { toast } from "sonner";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { copyText } from "@/lib/clipboard";

/** Wraps `children` (one element that can hold a ref) in a "Copy path" menu. */
export function CopyPathContextMenu({
  path,
  children,
}: {
  path: string;
  children: React.ReactElement;
}) {
  const copyPath = () =>
    void copyText(path).then(
      () => toast.success("Copied path"),
      () => toast.error("Couldn't copy the path"),
    );
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onSelect={copyPath}>
          <CopyIcon />
          Copy path
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  );
}
