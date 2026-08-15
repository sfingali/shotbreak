"""Hybrid co-reference resolution.

Stage A (deterministic, no LLM): normalize case/cue-suffixes, exact-match dedupe,
then rapidfuzz token-sort-ratio clustering — auto-merge above a high threshold.
Stage B (LLM, only for what's left): clusters that scored in the ambiguous band
(similar enough to flag, not similar enough to trust) go to the LLM for
adjudication with the full candidate list as context.

Elements are expected to already be deduplicated by exact literal name within a
(project, category) pair (breakdown_engine upserts on exact name) — this module
resolves the remaining spelling/casing/cue-suffix variants and writes the merge
into element / scene_element / element_alias.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from shotbreak.core.llm import provider as llm_provider

# Cue-only suffixes Fountain character cues carry that must not affect identity
# matching, e.g. "ALEX (O.S.)" and "ALEX" are the same character.
_CUE_SUFFIX_RE = re.compile(
    r"\s*\((?:O\.?S\.?|V\.?O\.?|CONT'?D\.?|OFF\s*SCREEN|OVER)\)\s*$",
    re.IGNORECASE,
)


def normalize_mention(name: str) -> str:
    """Strip cue-only suffixes and normalize whitespace/case for comparison."""
    cleaned = _CUE_SUFFIX_RE.sub("", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.upper()


@dataclass
class Cluster:
    canonical: str
    members: list[str] = field(default_factory=list)  # raw distinct names, size > 1 = a merge
    resolved_by: str = "exact"  # 'exact' | 'fuzzy' | 'llm'


@dataclass
class ClusterResult:
    clusters: list[Cluster]              # confidently resolved (including singletons)
    ambiguous_groups: list[list[str]]    # candidate groups needing LLM adjudication


def cluster_names(
    names: list[str],
    *,
    auto_merge_threshold: int = 92,
    candidate_threshold: int = 75,
) -> ClusterResult:
    """Stage A: deterministic clustering by normalized exact-match + rapidfuzz."""
    distinct = sorted(set(names))
    if not distinct:
        return ClusterResult(clusters=[], ambiguous_groups=[])

    norm_groups: dict[str, list[str]] = {}
    for n in distinct:
        norm_groups.setdefault(normalize_mention(n), []).append(n)

    norm_keys = list(norm_groups.keys())
    assigned: set[str] = set()
    clusters: list[Cluster] = []
    ambiguous_pairs: list[tuple[str, str]] = []

    for i, key in enumerate(norm_keys):
        if key in assigned:
            continue
        assigned.add(key)
        cluster_members = list(norm_groups[key])
        merged_any = len(norm_groups[key]) > 1

        for other_key in norm_keys[i + 1:]:
            if other_key in assigned:
                continue
            score = fuzz.token_sort_ratio(key, other_key)
            if score >= auto_merge_threshold:
                cluster_members.extend(norm_groups[other_key])
                assigned.add(other_key)
                merged_any = True
            elif score >= candidate_threshold:
                ambiguous_pairs.append((key, other_key))

        canonical = _pick_canonical(cluster_members)
        clusters.append(
            Cluster(
                canonical=canonical,
                members=sorted(set(cluster_members)),
                resolved_by="fuzzy" if merged_any else "exact",
            )
        )

    ambiguous_groups = _group_ambiguous_pairs(ambiguous_pairs, norm_groups)
    return ClusterResult(clusters=clusters, ambiguous_groups=ambiguous_groups)


def _pick_canonical(members: list[str]) -> str:
    """Prefer a non-ALL-CAPS form (screenplay cues are always caps; a title-cased
    mention, if one exists, reads better as the canonical label); shortest as tiebreak.
    """
    title_cased = [m for m in members if m != m.upper()]
    pool = title_cased or members
    return min(pool, key=lambda m: (len(m), m))


def _group_ambiguous_pairs(
    pairs: list[tuple[str, str]],
    norm_groups: dict[str, list[str]],
) -> list[list[str]]:
    """Union-find over ambiguous normalized-key pairs, expanded back to raw names."""
    parent: dict[str, str] = {k: k for k in norm_groups}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in pairs:
        union(a, b)

    groups: dict[str, list[str]] = {}
    involved = {k for pair in pairs for k in pair}
    for key in involved:
        groups.setdefault(find(key), []).extend(norm_groups[key])

    return [sorted(set(members)) for members in groups.values() if len(members) > 1]


def adjudicate_ambiguous_groups(
    config: dict,
    provider_name: str,
    category: str,
    ambiguous_groups: list[list[str]],
    context: str = "",
) -> tuple[list[list[str]], llm_provider.LLMResponse | None]:
    """Stage B: ask the LLM which ambiguous candidates genuinely co-refer.

    Returns (confirmed_groups, llm_response) — confirmed_groups is a list of
    same-entity name groups (size >= 2); anything the model doesn't confirm
    stays split (i.e. is simply absent from the result). llm_response is None
    if there was nothing to adjudicate.
    """
    if not ambiguous_groups:
        return [], None

    all_names = sorted({n for g in ambiguous_groups for n in g})
    schema = {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
            }
        },
        "required": ["groups"],
        "additionalProperties": False,
    }
    system = (
        "You are resolving name co-reference for a screenplay production breakdown, "
        f"category '{category}'. You are given clusters of candidate names that a "
        "fuzzy string match flagged as *possibly* the same entity but wasn't confident "
        "enough to merge automatically. Decide which names genuinely refer to the same "
        "entity (e.g. 'ALEX' and 'BENJAMIN' as the same character, vs 'ALEX' and "
        "'BENNY THE DOG' as different entities). Only group names you are confident "
        "refer to the same entity — when in doubt, keep them separate; a false split "
        "is much cheaper to fix than a false merge. "
        'Respond with JSON: {"groups": [[name, name, ...], ...]} — include ONLY groups '
        "of 2 or more confirmed-same-entity names; omit names that don't match anything."
    )
    user = (
        "Candidate clusters (grouped by fuzzy string match):\n"
        + "\n".join(f"- {g}" for g in ambiguous_groups)
        + (f"\n\nScene context:\n{context}\n" if context else "")
        + f"\nAll candidate names under consideration: {all_names}"
    )

    response = llm_provider.complete(
        config, provider_name, system, user, max_tokens=2048, json_schema=schema
    )
    data = json.loads(response.text)
    confirmed = [g for g in data.get("groups", []) if len(g) > 1]
    return confirmed, response


def _merge_scene_element_rows(conn: sqlite3.Connection, element_id: int) -> None:
    """Merge duplicate scene_element rows for an element after reassignment.

    Keeps the lowest id row per (scene_id, montage_beat_id) and folds the
    other rows' context/notes/quantity/ai_confidence into it so no tagged
    scene detail is lost when variants collapse into one element.
    """
    rows = conn.execute(
        "SELECT id, scene_id, montage_beat_id, context, quantity, notes, ai_confidence "
        "FROM scene_element WHERE element_id = ? "
        "ORDER BY scene_id, COALESCE(montage_beat_id, -1), id",
        (element_id,),
    ).fetchall()

    groups: dict[tuple[int, int | None], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["scene_id"], row["montage_beat_id"]), []).append(row)

    for group in groups.values():
        if len(group) < 2:
            continue

        keep = group[0]
        contexts = [r["context"] for r in group if r["context"]]
        notes = [r["notes"] for r in group if r["notes"]]
        quantities = [r["quantity"] for r in group if r["quantity"] is not None]
        confidences = [r["ai_confidence"] for r in group if r["ai_confidence"] is not None]

        conn.execute(
            "UPDATE scene_element SET context = ?, notes = ?, quantity = ?, ai_confidence = ? "
            "WHERE id = ?",
            (
                " | ".join(dict.fromkeys(contexts)) or keep["context"],
                " | ".join(dict.fromkeys(notes)) or keep["notes"],
                max(quantities) if quantities else keep["quantity"],
                max(confidences) if confidences else keep["ai_confidence"],
                keep["id"],
            ),
        )

        ids = [r["id"] for r in group[1:]]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM scene_element WHERE id IN ({placeholders})", ids)


def merge_elements(
    conn: sqlite3.Connection,
    project_id: int,
    category: str,
    cluster: Cluster,
) -> int | None:
    """Merge all element rows named in cluster.members (same project+category)
    into one canonical element. Writes element_alias rows for every member,
    reassigns dependent rows, and dedupes scene_element while preserving its
    fields. Returns the canonical element_id, or None if no matching rows were
    found.
    """
    if not cluster.members:
        return None

    placeholders = ",".join("?" for _ in cluster.members)
    rows = conn.execute(
        f"SELECT id, name FROM element WHERE project_id = ? AND category = ? "
        f"AND name IN ({placeholders})",
        (project_id, category, *cluster.members),
    ).fetchall()
    if not rows:
        return None

    canonical_row = next((r for r in rows if r["name"] == cluster.canonical), rows[0])
    canonical_id = canonical_row["id"]

    # Reassign or delete dependents before deleting the element rows, since
    # these tables have non-cascade FK REFERENCES element(id).
    element_dependent_tables = (
        ("physical_description", "element_id"),
        ("continuity_state", "element_id"),
        ("character_era", "element_id"),
        ("reference_image", "element_id"),
        ("element_alias", "element_id"),
    )

    for row in rows:
        if row["id"] == canonical_id:
            continue

        for table, column in element_dependent_tables:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                (canonical_id, row["id"]),
            )
        conn.execute(
            "UPDATE character_relationship SET element_id_a = ? WHERE element_id_a = ?",
            (canonical_id, row["id"]),
        )
        conn.execute(
            "UPDATE character_relationship SET element_id_b = ? WHERE element_id_b = ?",
            (canonical_id, row["id"]),
        )
        conn.execute(
            "UPDATE scene_element SET element_id = ? WHERE element_id = ?",
            (canonical_id, row["id"]),
        )
        conn.execute("DELETE FROM element WHERE id = ?", (row["id"],))

    # Collapse scene_element rows that are now duplicates, preserving the
    # merged fields rather than discarding all but MIN(id).
    _merge_scene_element_rows(conn, canonical_id)

    for member in cluster.members:
        conn.execute(
            "INSERT INTO element_alias (element_id, raw_mention, match_method, confidence) "
            "VALUES (?, ?, ?, ?)",
            (canonical_id, member, cluster.resolved_by, None),
        )

    return canonical_id


def resolve_project_coreferences(
    conn: sqlite3.Connection,
    config: dict,
    project_id: int,
    provider_name: str,
    *,
    auto_merge_threshold: int = 92,
    candidate_threshold: int = 75,
) -> dict:
    """Run Stage A + Stage B co-reference resolution across every category in
    the project and write the merges. Returns a summary dict for logging/cost
    tracking (the caller is responsible for writing llm_call_log rows, since it
    owns the breakdown_run_id).
    """
    categories = [
        r["category"]
        for r in conn.execute(
            "SELECT DISTINCT category FROM element WHERE project_id = ?", (project_id,)
        ).fetchall()
    ]

    summary = {
        "categories_processed": 0,
        "fuzzy_merges": 0,
        "llm_merges": 0,
        "llm_calls": [],  # list of llm_provider.LLMResponse, for the caller to cost-log
    }

    for category in categories:
        names = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM element WHERE project_id = ? AND category = ?",
                (project_id, category),
            ).fetchall()
        ]
        result = cluster_names(
            names,
            auto_merge_threshold=auto_merge_threshold,
            candidate_threshold=candidate_threshold,
        )
        summary["categories_processed"] += 1

        for cluster in result.clusters:
            if len(cluster.members) > 1:
                merge_elements(conn, project_id, category, cluster)
                summary["fuzzy_merges"] += 1

        if result.ambiguous_groups:
            confirmed, response = adjudicate_ambiguous_groups(
                config, provider_name, category, result.ambiguous_groups
            )
            if response is not None:
                summary["llm_calls"].append(response)
            for group in confirmed:
                cluster = Cluster(
                    canonical=_pick_canonical(group), members=group, resolved_by="llm"
                )
                merge_elements(conn, project_id, category, cluster)
                summary["llm_merges"] += 1

        conn.commit()

    return summary
