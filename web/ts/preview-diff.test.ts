// Tests for the live preview's changed-line detection.
import { changedLines } from "./preview-diff.js";

let failures = 0;
function eq(actual: Set<number>, expected: number[], msg: string) {
  const got = [...actual].sort((a, b) => a - b);
  if (JSON.stringify(got) !== JSON.stringify(expected)) {
    failures++;
    console.error(`✗ ${msg}: expected [${expected}], got [${got}]`);
  }
}
const lines = (s: string) => s.split("\n");

eq(changedLines(lines("a\nb\nc"), lines("a\nb\nc")), [], "identical text changes nothing");
eq(changedLines(lines("a\nb\nc"), lines("a\nB\nc")), [1], "an edited line is the only change");
eq(changedLines(lines("a\nb\nc"), lines("a\nx\ny\nb\nc")), [1, 2], "inserted lines don't mark the lines below them");
eq(changedLines(lines("a\nb\nc"), lines("a\nc")), [], "a removed line leaves nothing to highlight");
eq(changedLines([], lines("a\nb")), [0, 1], "everything is new after an empty preview");
eq(changedLines(lines("[X]\nA = 1\n\n[Y]\nB = 2"), lines("[X]\nA = 1\n\n[Z]\nC = 3\n\n[Y]\nB = 2")), [3, 4, 5], "a new section in the middle");

if (failures) {
  console.error(`\n${failures} preview-diff failure(s)`);
  process.exit(1);
}
console.log("✓ preview diff OK");
