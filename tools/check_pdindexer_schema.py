#!/usr/bin/env python3
"""Check whether PDIndexer's pinned Crystallography schema has drifted
away from utils/pdindexer/schema_snapshot.json.

Run this manually before a beamtime and whenever PDIndexer or IPAnalyzer
get updated (see docs/PLAN_PDINDEXER_BRIDGE.md §5). It is NOT part of the
regular test suite: it depends on network access to GitHub, and on
upstream's own activity, not on any code change in this repository.

The basis for comparison is PDIndexer's own Crystallography *gitlink* --
i.e. the exact commit PDIndexer's submodule pointer names -- not
Crystallography's own main branch HEAD. PDIndexer decides which upstream
schema it will actually try to deserialise against; watching main directly
would fire alarms early (before PDIndexer updates) or miss real drift
(after PDIndexer updates but before we notice). See §0.0 of the plan.

This is a targeted canary, not a C# parser: for each type, it looks for
each of the *known* (snapshot) member names as a whole-word declaration
site within that type's body, in source order, and reports whether the
order or presence changed. It checks NAMES AND ORDER ONLY -- it does not
parse or compare each member's *type*, so e.g. a upstream change from
`double NormarizeIntensity` to `float NormarizeIntensity` (same name, same
position, different wire size) would NOT be flagged, even though it would
break the byte layout. Any other declaration-like line in the type body
that matches neither a known member nor the small allowlist of known
non-members (_KNOWN_NON_MEMBER_LINES) is now treated as drift too (a
likely new field) -- previously this was only a printed NOTE that did not
affect the exit code, letting a real new member pass silently as long as
the already-known members still lined up (see code review 2026-09-06).
PointD is recorded in the snapshot for reference but not auto-verified
here (it is in a separate source file and is a simple, low-risk 2-member
type). XrayLine IS auto-verified (it has no explicit member values, so a
member inserted before Ka1 upstream would silently shift the wire value
PdiTypes.cs hardcodes for XrayLine.Ka1 even though the name is unchanged
-- an earlier version of this tool skipped it on the mistaken reasoning
that only sending Ka1 made its order irrelevant). Any enum this tool
fails to extract at all now counts as drift too, not just a warning --
"couldn't verify" must not exit 0 before a beamtime (see code review
2026-09-06). This tool is a best-effort trip-wire for the failure modes
that are cheap to catch by text matching, not a substitute for Phase 0's
golden-data verification
against a real capture.

Exit codes: 0 = no schema drift (gitlink may still have moved -- see
output), 1 = schema drift detected, 2 = could not reach GitHub.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

_SNAPSHOT_PATH = pathlib.Path(__file__).resolve().parent.parent / "utils" / "pdindexer" / "schema_snapshot.json"
_API = "https://api.github.com"
_RAW = "https://raw.githubusercontent.com"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "bl18c_controller-schema-check"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "bl18c_controller-schema-check"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")


def _gitlink_sha(repo: str, submodule_path: str, ref: str = "HEAD") -> str:
    tree = _get_json(f"{_API}/repos/{repo}/git/trees/{ref}?recursive=1")
    for entry in tree.get("tree", []):
        if entry.get("mode") == "160000" and entry.get("path") == submodule_path:
            return entry["sha"]
    raise RuntimeError(f"submodule {submodule_path!r} not found in {repo}@{ref}")


def _head_sha(repo: str, branch: str) -> str:
    return _get_json(f"{_API}/repos/{repo}/commits/{branch}")["sha"]


# ---------------------------------------------------------------------------
# Name-anchored member-order extraction (see module docstring for the
# "canary, not a parser" scoping rationale).
# ---------------------------------------------------------------------------

# Declaration lines this tool already knows are legitimately NOT
# MemoryPack members (matched by _extract_member_order, see there) — an
# "unexplained" line outside this allowlist is now treated as drift, not
# just a printed NOTE that exit code 0 ignored (see code review
# 2026-09-06: a genuinely new member used to pass silently as long as the
# already-known 55/4/15 member sets still matched).
_KNOWN_NON_MEMBER_LINES = {
    "DiffractionProfile2": {"public static byte ID => 2;"},
    "Profile": {"public Color Color = Color.Blue;"},
    "HorizontalAxisProperty": set(),
}

_TYPE_DECL_RE_TMPL = r"public\s+partial\s+(?:class|record\s+struct|struct)\s+{name}\b"


def _type_body(source: str, type_name: str) -> str:
    m = re.search(_TYPE_DECL_RE_TMPL.format(name=re.escape(type_name)), source)
    if not m:
        raise RuntimeError(f"could not find declaration of {type_name}")
    rest = source[m.start():]
    end = rest.index("\n}\n")
    return rest[: end + 2]


def _depth1_lines(type_body: str) -> list[str]:
    """Lines at brace-depth 1 (i.e. immediately inside the type's own
    opening brace), stripped of comments/attributes/region directives/pure
    brace lines. Depth is computed by naive per-character {/} counting,
    which this module's caller has verified nets to exactly 0 over a full
    type body for this specific upstream file (no unbalanced braces inside
    string/interpolation literals at the type-body level)."""
    depth = 0
    kept = []
    for line in type_body.splitlines():
        d_before = depth
        stripped = line.strip()
        if d_before == 1 and stripped and not stripped.startswith(("//", "[", "{", "}", "#")):
            kept.append(stripped)
        depth += line.count("{") - line.count("}")
    return kept


def _extract_member_order(source: str, type_name: str, known_members: list[str]) -> tuple[list[str], list[str]]:
    """Returns (order_found, unexplained_lines). order_found is the subset
    of known_members present, in the order their declaration first appears
    (a declared NAME is required to be followed by =, `,`, `;`, `{`, or
    end-of-line -- never by another identifier char, which would mean the
    match is a TYPE token rather than the member's own name; this
    specifically disambiguates members whose name equals their own type,
    e.g. DiffractionProfile2.Profile : Profile, or
    HorizontalAxisProperty.WaveSource : WaveSource)."""
    body = _type_body(source, type_name)
    kept_lines = _depth1_lines(body)
    joined = "\n".join(kept_lines)

    positions: dict[str, int] = {}
    for name in known_members:
        pattern = rf"(?<!\.)\b{re.escape(name)}\b(?=\s*[=,;{{]|\s*$)"
        m = re.search(pattern, joined, flags=re.MULTILINE)
        if m:
            positions[name] = m.start()
    order = sorted(positions, key=positions.get)

    unexplained = []
    for line in kept_lines:
        if line.startswith(("private", "static")):
            continue
        if "(" in line:
            continue
        if not re.search(r"[;{]", line):
            continue
        if any(re.search(rf"\b{re.escape(n)}\b", line) for n in known_members):
            continue
        unexplained.append(line)
    return order, unexplained


def _extract_enum(source: str, enum_name: str) -> list[str]:
    m = re.search(rf"public enum {re.escape(enum_name)}\s*\{{([^}}]*)\}}", source)
    if not m:
        raise RuntimeError(f"enum {enum_name} not found")
    return [tok.strip() for tok in m.group(1).split(",") if tok.strip()]


def main() -> int:
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    basis = snapshot["basis"]

    print("Resolving current upstream commits...")
    try:
        pdi_head = _head_sha("seto77/PDIndexer", "master")
        crys_sha = _gitlink_sha("seto77/PDIndexer", "Crystallography")
        ipa_head = _head_sha("seto77/IPAnalyzer", "master")
        ipa_crys_sha = _gitlink_sha("seto77/IPAnalyzer", "Crystallography")
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"ERROR: could not reach GitHub: {exc}", file=sys.stderr)
        return 2

    print(f"  PDIndexer HEAD:                  {pdi_head}")
    print(f"  PDIndexer -> Crystallography:     {crys_sha}")
    print(f"  IPAnalyzer HEAD:                 {ipa_head}")
    print(f"  IPAnalyzer -> Crystallography:    {ipa_crys_sha}")
    print()

    if crys_sha != basis["crystallography_gitlink_sha"]:
        print("NOTE: PDIndexer's Crystallography gitlink has moved since the snapshot")
        print(f"      was captured ({basis['crystallography_gitlink_sha']} -> {crys_sha}).")
        print("      This is informational, not itself a failure -- checking the schema")
        print("      AT THE NEW SHA below is what actually matters.")
        print()
    if ipa_crys_sha != basis["ipanalyzer_crystallography_gitlink_sha"]:
        print("NOTE: IPAnalyzer's Crystallography gitlink differs from the snapshot too")
        print(f"      ({basis['ipanalyzer_crystallography_gitlink_sha']} -> {ipa_crys_sha}).")
        print("      PDIndexer and IPAnalyzer pointing at different commits is normal --")
        print("      see docs/PLAN_PDINDEXER_BRIDGE.md §0.0. Only the PDIndexer side (above)")
        print("      is our compatibility basis.")
        print()

    print(f"Fetching Profile.cs / Enums.cs / UniversalConstants.cs at {crys_sha}...")
    try:
        profile_cs = _get_text(f"{_RAW}/seto77/Crystallography/{crys_sha}/Profile.cs")
        enums_cs = _get_text(f"{_RAW}/seto77/Crystallography/{crys_sha}/Enums.cs")
        universal_constants_cs = _get_text(f"{_RAW}/seto77/Crystallography/{crys_sha}/UniversalConstants.cs")
    except urllib.error.URLError as exc:
        print(f"ERROR: could not fetch source: {exc}", file=sys.stderr)
        return 2

    drift = False

    for type_name in ("DiffractionProfile2", "Profile", "HorizontalAxisProperty"):
        expected = snapshot["types"][type_name]["members"]
        try:
            order, unexplained = _extract_member_order(profile_cs, type_name, expected)
        except RuntimeError as exc:
            drift = True
            print(f"DRIFT: could not locate/parse {type_name}: {exc}")
            continue

        if order != expected:
            drift = True
            print(f"DRIFT: {type_name} member list/order changed:")
            print(f"  snapshot ({len(expected)}): {expected}")
            print(f"  live     ({len(order)}): {order}")
        else:
            print(f"OK: {type_name} — {len(order)} members, unchanged.")

        allowed = _KNOWN_NON_MEMBER_LINES.get(type_name, set())
        newly_unexplained = [line for line in unexplained if line not in allowed]
        if newly_unexplained:
            drift = True
            print(f"DRIFT: {type_name} has {len(newly_unexplained)} declaration-like line(s) "
                  f"that are neither a known member nor a known non-member — likely a new field:")
            for line in newly_unexplained:
                print(f"    {line}")

    def _check_enums(source: str, enum_names: tuple[str, ...]) -> None:
        nonlocal drift
        for enum_name in enum_names:
            try:
                live_members = _extract_enum(source, enum_name)
            except RuntimeError as exc:
                # Could not verify at all -- e.g. the enum was renamed or
                # restructured beyond what this regex handles. Treat as
                # drift rather than a silent pass: "couldn't confirm this
                # is safe" must not exit 0 before a beamtime. An earlier
                # version only printed a WARN here and kept exit code 0
                # (see code review 2026-09-06).
                drift = True
                print(f"DRIFT: could not extract enum {enum_name}: {exc}")
                continue
            expected = snapshot["enums"][enum_name]
            if live_members != expected:
                drift = True
                print(f"DRIFT: enum {enum_name} changed: snapshot={expected} live={live_members}")
            else:
                print(f"OK: enum {enum_name} — unchanged.")

    _check_enums(profile_cs, ("HorizontalAxis", "WaveSource", "WaveColor", "DiffractionProfileMode", "BackgroundMode"))
    _check_enums(enums_cs, ("AngleUnitEnum", "LengthUnitEnum", "EnergyUnitEnum", "TimeUnitEnum"))
    # XrayLine's members have no explicit values (Ka=0, Ka1=1, Ka2=2, ...) --
    # only sent as an int, so a member INSERTED before Ka1 in upstream's
    # declaration would silently shift Ka1's wire value out from under our
    # mirror (PdiTypes.cs hardcodes XrayLine.Ka1) even though the name
    # "Ka1" still exists. An earlier version of this tool skipped XrayLine
    # entirely, reasoning that "only XrayLine.Ka1 is ever sent" made its
    # order irrelevant -- backwards: that's exactly why the order matters.
    # See code review 2026-09-06.
    _check_enums(universal_constants_cs, ("XrayLine",))

    print()
    print("(PointD is recorded in the snapshot for reference but not auto-verified")
    print(" by this script -- it is a simple 2-member type in a separate file.")
    print(" Spot-check by hand if in doubt.)")
    print()

    if drift:
        print("RESULT: schema drift detected. Update utils/pdindexer/csharp/PdiTypes.cs")
        print("        and utils/pdindexer/schema_snapshot.json, then re-run Phase 0's")
        print("        golden-data verification before trusting the bridge again.")
        return 1

    print("RESULT: no schema drift. If gitlink SHAs moved above, refresh")
    print("        schema_snapshot.json's 'basis' block to the new SHAs so future")
    print("        runs report a clean baseline (no code change needed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
