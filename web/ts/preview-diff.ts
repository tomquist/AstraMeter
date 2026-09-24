// preview-diff.ts — which lines of the generated config changed since the last
// render, so the live preview can point at what an answer just did. A plain
// longest-common-subsequence over lines: configs are a few hundred lines at
// most, and an insertion only marks the inserted lines, not everything below.

/** Indices into `next` of the lines that are not carried over from `prev`. */
export function changedLines(prev: string[], next: string[]): Set<number> {
  const n = prev.length;
  const m = next.length;
  // lcs[i][j] = length of the LCS of prev[i..] and next[j..]
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = prev[i] === next[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const changed = new Set<number>();
  let i = 0;
  let j = 0;
  while (j < m) {
    if (i < n && prev[i] === next[j]) {
      i++;
      j++;
    } else if (i < n && lcs[i + 1][j] >= lcs[i][j + 1]) {
      i++;
    } else {
      changed.add(j);
      j++;
    }
  }
  return changed;
}
