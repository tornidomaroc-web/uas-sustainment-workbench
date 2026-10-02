// What a Claude Code cloud session may not do in this repository.
//
// .claude/settings.json runs this file before every Bash command and file edit, and only when
// CLAUDE_CODE_REMOTE is "true": Claude Code sets it in cloud sessions and never locally, so the
// owner's local sessions never load this file at all. In a cloud session it refuses, with exit
// code 2 and a reason on stderr (which Claude Code shows the model):
//
//   merges              gh pr merge; gh api writes (-X/--method POST|PUT|PATCH|DELETE, -f, -F,
//                       --field, --raw-field, --input), which is how a merge or a ref change
//                       goes through the API
//   releases            gh release, except view, list and download
//   tags                git tag that creates, moves or deletes one (listing is fine); any push
//                       of tags (--tags, --follow-tags, refs/tags/)
//   pushes              to main or master, forced (--force, -f, --force-with-lease, +refspec),
//                       deleting (--delete, -d, :branch), --mirror, --all
//   docker and ollama   any docker, docker-compose, podman or ollama command, or the local
//                       model server's port 11434
//   the guard itself    an edit to anything under .claude/, by an edit tool or a shell command
//                       that writes there
//
// Everything else passes, including pushing the session's own branch and opening a PR, which is
// how a cloud session delivers its work. The owner merges, tags and releases locally.
//
// This is a convenience in front of the server-side rules, not a wall: it reads command text,
// and a command it does not recognise (a script, an alias, an indirection) passes. The rules
// that hold whatever a session runs are on GitHub: the branch rule on main and the v* tag
// rulesets. The cloud proxy also refuses tag pushes and branch deletions on its own.
"use strict";

const fs = require("fs");

function refuse(reason) {
  process.stderr.write(
    `cloud-guard: ${reason}. In this repository a cloud session prepares a branch and a PR; ` +
      "merges, tags, releases, docker and ollama stay with the owner's local sessions " +
      "(.claude/hooks/cloud-guard.js).\n",
  );
  process.exit(2);
}

let input;
try {
  input = JSON.parse(fs.readFileSync(0, "utf8"));
} catch (err) {
  refuse("the hook input could not be read, so the call is refused rather than waved through");
}

const tool = String(input.tool_name || "");
const args = input.tool_input || {};
const UNDER_CLAUDE = /(^|[\\/])\.claude[\\/]/;

if (["Edit", "Write", "MultiEdit", "NotebookEdit"].includes(tool)) {
  const path = String(args.file_path || args.notebook_path || "");
  if (UNDER_CLAUDE.test(path)) refuse(`an edit to ${path}, under .claude/, which holds this guard`);
  process.exit(0);
}

if (tool !== "Bash") process.exit(0);
const command = String(args.command || "");

// Each git invocation, with any "git -C <dir>" or "git -c k=v" options before the subcommand
// taken out, and its arguments up to the next ; & | or newline.
function gitCalls(sub) {
  const out = [];
  const re = new RegExp(`\\bgit((?:\\s+-[Cc]\\s+\\S+)*)\\s+${sub}\\b([^;&|\\n]*)`, "g");
  let m;
  while ((m = re.exec(command)) !== null) out.push(m[2].trim());
  return out;
}

const RULES = [
  [/\bgh\s+pr\s+merge\b/, "gh pr merge: merging is the owner's"],
  [/\bgh\s+release\b(?!\s+(view|list|download)\b)/, "gh release: releases are the owner's"],
  [
    /\bgh\s+api\b[^;&|\n]*\s(-X|--method)\s*=?\s*['"]?(POST|PUT|PATCH|DELETE)\b/i,
    "gh api with a writing method",
  ],
  [/\bgh\s+api\b[^;&|\n]*\s(-f|-F|--field|--raw-field|--input)(\s|=|$)/, "gh api with fields, which posts"],
  [/\b(docker|docker-compose|podman)\b/, "docker"],
  [/\bollama\b|\b11434\b/, "ollama or its local server"],
];
for (const [re, reason] of RULES) if (re.test(command)) refuse(reason);

for (const a of gitCalls("tag")) {
  const listing = a === "" || /^(-l|--list)\b/.test(a);
  if (!listing) refuse(`git tag ${a}: creating, moving or deleting a tag is the owner's`);
}

for (const a of gitCalls("push")) {
  const words = a.split(/\s+/).filter(Boolean);
  if (/(^|\s)--(tags|follow-tags|mirror|all|delete)\b/.test(a)) refuse(`git push ${a}`);
  if (/(^|\s)(-f|-d|--force|--force-with-lease|--force-if-includes)(\s|=|$)/.test(a)) refuse(`git push ${a}: forced or deleting`);
  if (/refs\/tags\//.test(a)) refuse(`git push ${a}: pushes a tag`);
  const refspecs = words.filter((w) => !w.startsWith("-")).slice(1); // after the remote
  for (const spec of refspecs) {
    const dest = spec.includes(":") ? spec.split(":").pop() : spec;
    if (spec.startsWith("+")) refuse(`git push ${a}: a forced refspec`);
    if (spec.startsWith(":")) refuse(`git push ${a}: deletes a branch`);
    if (/^(refs\/heads\/)?(main|master)$/.test(dest)) refuse(`git push ${a}: pushes to ${dest}`);
    if (/^v\d/.test(dest)) refuse(`git push ${a}: pushes a release tag`);
  }
}

// A shell command that writes under .claude/: rm, mv, cp, tee, sed -i, truncate, chmod, a
// redirect, or git rm, mv, checkout or restore. Reading it (cat, grep, git diff) is fine.
if (/\.claude[\\/]/.test(command)) {
  const writes =
    /(^|[;&|]\s*|\s)(rm|mv|cp|tee|truncate|chmod|ln)\s/.test(command) ||
    /\bsed\b[^;&|\n]*\s-i/.test(command) ||
    />\s*\S*\.claude[\\/]/.test(command) ||
    /\bgit\s+(rm|mv|checkout|restore)\b/.test(command);
  if (writes) refuse("a shell command that writes under .claude/, which holds this guard");
}

process.exit(0);
