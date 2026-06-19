// Entry point for the TipTap vendor bundle.
//
// Run scripts/build-tiptap-vendor.sh to produce frontend/vendor/tiptap.bundle.js,
// which exposes a single `window.Tiptap` namespace the Compose app uses:
//
//   const { Editor, StarterKit, Mention, Table, ... } = window.Tiptap;
//
// End users never run this — the bundle is committed to the repo. Only
// re-run when bumping TipTap versions or adding extensions.

import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import Underline from "@tiptap/extension-underline";
import Link from "@tiptap/extension-link";
import Placeholder from "@tiptap/extension-placeholder";
import Mention from "@tiptap/extension-mention";
import Table from "@tiptap/extension-table";
import TableRow from "@tiptap/extension-table-row";
import TableHeader from "@tiptap/extension-table-header";
import TableCell from "@tiptap/extension-table-cell";

export {
  Editor,
  StarterKit,
  Underline,
  Link,
  Placeholder,
  Mention,
  Table,
  TableRow,
  TableHeader,
  TableCell,
};
