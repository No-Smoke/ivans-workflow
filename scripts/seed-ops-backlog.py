#!/usr/bin/env python3
"""Seed the IWO Ops Actions backlog by scanning all handoff files.

Scans docs/agent-comms/{SPEC-ID}/LATEST.json for each spec, extracting
manual operational tasks from:
  - status.unresolvedIssues
  - deploymentInstructions.{preDeploySteps,postDeploySteps,manualSteps}
  - nextAgent.action (when target is "human")
  - Infrastructure flags (noNewMigrations=false → migration action)

Deduplicates via fingerprinting and classifies priority/category automatically.

Usage:
    python scripts/seed-ops-backlog.py [--project-root /path/to/ebatt]
    python scripts/seed-ops-backlog.py --dry-run
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from iwo.ops_actions import (
    OpsAction,
    OpsActionsRegister,
    classify_category,
    classify_priority,
    compute_fingerprint,
    _next_id,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seed-ops-backlog")


def extract_from_handoff(raw: dict, spec_id: str, filename: str) -> list[dict]:
    """Extract candidate ops actions from a single handoff JSON.
    
    Only extracts items that are clearly human operational tasks:
    migrations, secrets, DNS, deployments, infrastructure configuration.
    Filters out code review notes, technical debt, and verification suggestions.
    """
    candidates: list[dict] = []
    source_agent = raw.get("metadata", {}).get("agent", "unknown")
    sequence = raw.get("metadata", {}).get("sequence", 0)

    # Operational action patterns — things humans must DO (not code issues)
    OPS_PATTERNS = [
        re.compile(p, re.IGNORECASE) for p in [
            r'migration.*not\s+(yet\s+)?(applied|run)',
            r'not\s+yet\s+(set|configured|created|applied|deployed|stored)',
            r'wrangler\s+(secret|d1)',
            r'must\s+(run|create|configure|set|apply|execute|deploy|store)',
            r'secrets?\s+not\s+(set|configured)',
            r'\bDNS\b.*\b(CNAME|A\s+record|MX|DKIM|SPF|DMARC)\b',
            r'HUMAN\s+ACTION\s+REQUIRED',
            r'human\s+(must|task)',
            r'seed\s+data\s+not\s+(yet\s+)?loaded',
            r'not\s+yet\s+deployed',
            r'webhook.*not\s+(set|configured)',
            r'SMTP.*not\s+configured',
            r'Stripe\s+(product|webhook).*not\s+(yet\s+)?(created|configured)',
            r'n8n\s+workflow',
            r'Bitwarden',
            r'KV\s+namespace.*not\s+(yet\s+)?created',
            r'KV\s+namespace\s+ID\s+(is\s+)?empty',
            r'npx\s+wrangler',
            r'UPDATE\s+users\s+SET',
            r'sample.*report.*pdf.*not\s+exist',
            r'sample.*report.*pdf.*does\s+not\s+exist',
            r'PWA\s+icons?\s+are\s+placeholder',
            r'CloudFlare\s+dashboard',
        ]
    ]

    def is_ops_action(text: str) -> bool:
        """Check if text matches an operational action pattern."""
        return any(p.search(text) for p in OPS_PATTERNS)

    # 1. unresolvedIssues — only if they match ops patterns
    for issue in raw.get("status", {}).get("unresolvedIssues", []):
        if isinstance(issue, str) and len(issue.strip()) > 10 and is_ops_action(issue):
            candidates.append({
                "text": issue.strip(),
                "source": "unresolvedIssues",
                "source_agent": source_agent,
                "source_sequence": sequence,
            })

    # 2. deploymentInstructions sub-fields — only manual steps
    deploy_info = raw.get("deploymentInstructions", {})
    if isinstance(deploy_info, dict):
        for field_name in ("preDeploySteps", "postDeploySteps", "manualSteps"):
            steps = deploy_info.get(field_name, [])
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, str) and len(step.strip()) > 10 and is_ops_action(step):
                        candidates.append({
                            "text": step.strip(),
                            "source": f"deploymentInstructions.{field_name}",
                            "source_agent": source_agent,
                            "source_sequence": sequence,
                        })

    # 3. nextAgent.action when target is "human" — only ops-like actions
    next_agent = raw.get("nextAgent", {})
    if isinstance(next_agent, dict):
        target = next_agent.get("target", "")
        action_text = next_agent.get("action", "")
        if target == "human" and isinstance(action_text, str) and len(action_text.strip()) > 10:
            if is_ops_action(action_text):
                candidates.append({
                    "text": action_text.strip(),
                    "source": "nextAgent.action (human target)",
                    "source_agent": source_agent,
                    "source_sequence": sequence,
                })

    # 4. Infrastructure flags check
    if isinstance(deploy_info, dict):
        if deploy_info.get("noNewMigrations") is False:
            candidates.append({
                "text": f"D1 migration required for {spec_id} — noNewMigrations=false",
                "source": "deploymentInstructions.noNewMigrations",
                "source_agent": source_agent,
                "source_sequence": sequence,
            })
        if deploy_info.get("noNewSecrets") is False:
            candidates.append({
                "text": f"Wrangler secrets must be configured for {spec_id} — noNewSecrets=false",
                "source": "deploymentInstructions.noNewSecrets",
                "source_agent": source_agent,
                "source_sequence": sequence,
            })

    return candidates


def make_title(text: str) -> str:
    """Extract a short title from action text (first sentence or first 80 chars)."""
    # First sentence
    for sep in [". ", " — ", " - ", "\n"]:
        if sep in text:
            title = text[:text.index(sep)]
            if len(title) > 15:
                return title[:80]
    # Truncate
    if len(text) > 80:
        return text[:77] + "..."
    return text


def scan_handoffs(handoffs_dir: Path) -> list[dict]:
    """Scan all spec directories and extract candidate actions."""
    all_candidates: list[dict] = []

    for spec_dir in sorted(handoffs_dir.iterdir()):
        if not spec_dir.is_dir():
            continue
        if spec_dir.name.startswith("."):
            continue

        spec_id = spec_dir.name

        # Scan ALL JSON files (not just LATEST) to catch historical actions
        json_files = sorted(spec_dir.glob("*.json"))
        json_files = [
            f for f in json_files
            if f.name != "LATEST.json"
            and not f.name.endswith(".tmp")
        ]

        for json_file in json_files:
            try:
                with open(json_file) as fh:
                    raw = json.load(fh)
                candidates = extract_from_handoff(raw, spec_id, json_file.name)
                for c in candidates:
                    c["spec_id"] = spec_id
                    c["filename"] = json_file.name
                all_candidates.extend(candidates)
            except Exception as e:
                log.warning(f"Failed to parse {json_file}: {e}")

    return all_candidates


def build_actions(candidates: list[dict]) -> list[OpsAction]:
    """Convert raw candidates into deduplicated OpsAction objects."""
    seen_fingerprints: set[str] = set()
    actions: list[OpsAction] = []
    id_counter = 0

    for c in candidates:
        spec_id = c["spec_id"]
        text = c["text"]
        fp = compute_fingerprint(spec_id, text)

        if fp in seen_fingerprints:
            continue
        seen_fingerprints.add(fp)

        id_counter += 1
        priority = classify_priority(text)
        category = classify_category(text)
        title = make_title(text)

        action = OpsAction(
            id=f"ops-seed-{id_counter:03d}",
            spec_id=spec_id,
            title=title,
            description=text,
            category=category,
            priority=priority,
            status="pending",
            source_agent=c.get("source_agent", "unknown"),
            source_sequence=c.get("source_sequence", 0),
            fingerprint=fp,
            auto_extracted=True,
        )
        actions.append(action)

    return actions


def main():
    parser = argparse.ArgumentParser(description="Seed IWO Ops Actions backlog")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.home() / "Nextcloud/PROJECTS/ebatt-ai/ebatt",
        help="Path to ebatt project root",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show actions that would be created without writing",
    )
    args = parser.parse_args()

    handoffs_dir = args.project_root / "docs" / "agent-comms"
    ops_file = handoffs_dir / ".ops-actions.json"

    if not handoffs_dir.exists():
        log.error(f"Handoffs directory not found: {handoffs_dir}")
        sys.exit(1)

    log.info(f"Scanning handoffs in: {handoffs_dir}")
    candidates = scan_handoffs(handoffs_dir)
    log.info(f"Found {len(candidates)} raw candidates across all specs")

    actions = build_actions(candidates)
    log.info(f"Deduplicated to {len(actions)} unique ops actions")

    # Summary by priority
    by_priority = {"critical": 0, "warning": 0, "info": 0}
    for a in actions:
        by_priority[a.priority] = by_priority.get(a.priority, 0) + 1
    log.info(f"Priority breakdown: {by_priority['critical']} critical, "
             f"{by_priority['warning']} warning, {by_priority['info']} info")

    # Summary by category
    by_category: dict[str, int] = {}
    for a in actions:
        by_category[a.category] = by_category.get(a.category, 0) + 1
    log.info(f"Category breakdown: {dict(sorted(by_category.items()))}")

    if args.dry_run:
        print("\n--- DRY RUN: Actions that would be created ---\n")
        for a in actions:
            print(f"[{a.priority:8s}] [{a.category:12s}] {a.spec_id:30s} | {a.title}")
        print(f"\nTotal: {len(actions)} actions")
        return

    # Load existing register (for idempotent re-runs)
    register = OpsActionsRegister(ops_file)
    register.load()
    existing_count = len(register.actions)

    added = 0
    for action in actions:
        if register.add(action):
            added += 1

    register.save()
    log.info(f"Seeding complete: {added} new actions added "
             f"(was {existing_count}, now {len(register.actions)})")

    # Print report
    print(f"\n{'='*70}")
    print(f"OPS ACTIONS BACKLOG SEEDED")
    print(f"{'='*70}")
    print(f"File: {ops_file}")
    print(f"Total actions: {len(register.actions)}")
    print(f"New this run: {added}")
    print()

    summary = register.get_summary()
    for priority in ("critical", "warning", "info"):
        if priority in summary:
            counts = summary[priority]
            pending = counts.get("pending", 0)
            completed = counts.get("completed", 0)
            skipped = counts.get("skipped", 0)
            print(f"  {priority:8s}: {pending} pending, {completed} completed, {skipped} skipped")

    pending = register.get_pending()
    if pending:
        print(f"\n--- Pending Actions ({len(pending)}) ---\n")
        for a in pending:
            stale_marker = " [STALE]" if a.stale_since else ""
            print(f"  {a.id:16s} [{a.priority:8s}] [{a.category:12s}] {a.spec_id}")
            print(f"  {'':16s} {a.title}{stale_marker}")
            print()


if __name__ == "__main__":
    main()
