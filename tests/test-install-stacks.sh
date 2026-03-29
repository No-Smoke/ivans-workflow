#!/usr/bin/env bash
# Test Ivan's Workflow install.sh across different stack combinations
# Verifies: LANG_EXT derivation, SOURCE_DIR, all template substitutions
# Usage: bash tests/test-install-stacks.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRAMEWORK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_SCRIPT="$FRAMEWORK_DIR/install.sh"
TMPDIR_BASE=$(mktemp -d /tmp/ivans-workflow-test-XXXXXX)
PASS=0
FAIL=0
ERRORS=""

cleanup() {
    rm -rf "$TMPDIR_BASE"
}
trap cleanup EXIT

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

# ── Test Cases ─────────────────────────────────────────────────
# Format: "name|runtime|framework|language|expected_ext|expected_test|expected_lint"
TEST_CASES=(
    "cf-hono-ts|cloudflare-workers|hono|typescript|ts|npm test|npm run lint"
    "node-express-ts|node|express|typescript|ts|npm test|npm run lint"
    "node-express-js|node|express|javascript|js|npm test|npm run lint"
    "python-fastapi|python|fastapi|python|py|pytest|ruff check ."
    "cf-nextjs-ts|cloudflare-workers|nextjs|typescript|ts|npm test|npm run lint"
    "deno-none-ts|deno|none|typescript|ts|npm test|npm run lint"
)

# ── Non-interactive install function ───────────────────────────
run_install_test() {
    local test_name="$1" runtime="$2" framework="$3" language="$4"
    local expected_ext="$5" expected_test="$6" expected_lint="$7"
    
    local target="$TMPDIR_BASE/$test_name"
    mkdir -p "$target"
    
    # --- Replicate install.sh variable derivation ---
    local RUNTIME="$runtime"
    local FRAMEWORK="$framework"
    local LANGUAGE="$language"
    local PROJECT_NAME="test-$test_name"
    local PROJECT_DESCRIPTION="Test project for $test_name"
    local LINK_MODE="copy"
    local SCHEMA_ENABLED="false"
    local SCHEMA_DEFINITIONS=""
    local SCHEMA_GENERATED=""
    local SCHEMA_GEN_CMD=""
    local SCHEMA_CONFIG=""
    local SPECS_ENABLED="false"
    local SPEC_DIRS='["specs/"]'
    local SPEC_PREFIX="PROJ"
    local AGENT_COUNT=4
    local BUILDER_MODE="auto-accept"
    local CREDENTIALS_YAML="[]"
    local HANDOFFS_DIR="docs/agent-comms"
    local COVERAGE_THRESHOLD=80
    local TEST_RUNNER LINTER FORMATTER TYPECHECK_CMD TEST_CMD LINT_CMD DEPLOY_CMD DEV_CMD LANG_EXT SOURCE_DIR TESTS_DIR

    case "$LANGUAGE" in
        typescript)
            TEST_RUNNER="vitest"; LINTER="eslint"; FORMATTER="prettier"
            TYPECHECK_CMD="npx tsc --noEmit"; TEST_CMD="npm test"; LINT_CMD="npm run lint"
            ;;
        javascript)
            TEST_RUNNER="jest"; LINTER="eslint"; FORMATTER="prettier"
            TYPECHECK_CMD=""; TEST_CMD="npm test"; LINT_CMD="npm run lint"
            ;;
        python)
            TEST_RUNNER="pytest"; LINTER="ruff"; FORMATTER="black"
            TYPECHECK_CMD=""; TEST_CMD="pytest"; LINT_CMD="ruff check ."
            ;;
    esac

    case "$RUNTIME" in
        cloudflare-workers) DEPLOY_CMD="npx wrangler deploy"; DEV_CMD="npx wrangler dev" ;;
        node) DEPLOY_CMD=""; DEV_CMD="npm run dev" ;;
        python) DEPLOY_CMD=""; DEV_CMD="python -m uvicorn main:app --reload" ;;
        *) DEPLOY_CMD=""; DEV_CMD="npm run dev" ;;
    esac

    case "$LANGUAGE" in
        typescript) LANG_EXT="ts" ;;
        javascript) LANG_EXT="js" ;;
        python)     LANG_EXT="py" ;;
        *)          LANG_EXT="*" ;;
    esac

    case "$FRAMEWORK" in
        nextjs) SOURCE_DIR="src"; TESTS_DIR="__tests__" ;;
        *) SOURCE_DIR="src"; TESTS_DIR="tests" ;;
    esac

    # --- Generate files using sed (same as install.sh) ---
    local claude_dir="$target/.claude"
    mkdir -p "$claude_dir/rules"
    
    # 1. project-stack.md
    sed \
        -e "s|\${RUNTIME}|$RUNTIME|g" \
        -e "s|\${FRAMEWORK}|$FRAMEWORK|g" \
        -e "s|\${LANGUAGE}|$LANGUAGE|g" \
        -e "s|\${LANG_EXT}|$LANG_EXT|g" \
        -e "s|\${SOURCE_DIR}|$SOURCE_DIR|g" \
        "$FRAMEWORK_DIR/templates/rules/project-stack.md.tmpl" > "$claude_dir/rules/project-stack.md"

    # 2. project-domain.md
    sed \
        -e "s|\${PROJECT_NAME}|$PROJECT_NAME|g" \
        "$FRAMEWORK_DIR/templates/rules/project-domain.md.tmpl" > "$claude_dir/rules/project-domain.md"

    # 3. CLAUDE.md
    sed \
        -e "s|\${PROJECT_NAME}|$PROJECT_NAME|g" \
        -e "s|\${PROJECT_DESCRIPTION}|$PROJECT_DESCRIPTION|g" \
        -e "s|\${RUNTIME}|$RUNTIME|g" \
        -e "s|\${FRAMEWORK}|$FRAMEWORK|g" \
        -e "s|\${LANGUAGE}|$LANGUAGE|g" \
        -e "s|\${TYPECHECK_CMD}|$TYPECHECK_CMD|g" \
        -e "s|\${TEST_CMD}|$TEST_CMD|g" \
        -e "s|\${LINT_CMD}|$LINT_CMD|g" \
        -e "s|\${SOURCE_DIR}|$SOURCE_DIR|g" \
        -e "s|\${TESTS_DIR}|$TESTS_DIR|g" \
        -e "s|\${COVERAGE_THRESHOLD}|$COVERAGE_THRESHOLD|g" \
        -e "s|\${DEPLOY_CMD}|$DEPLOY_CMD|g" \
        -e "s|\${DEV_CMD}|$DEV_CMD|g" \
        -e "s|\${TEST_RUNNER}|$TEST_RUNNER|g" \
        -e "s|\${LINTER}|$LINTER|g" \
        -e "s|\${FORMATTER}|$FORMATTER|g" \
        -e "s|\${HANDOFFS_DIR}|$HANDOFFS_DIR|g" \
        -e "s|\${SCHEMA_ENABLED}|$SCHEMA_ENABLED|g" \
        -e "s|\${SCHEMA_DEFINITIONS}|$SCHEMA_DEFINITIONS|g" \
        -e "s|\${SCHEMA_GENERATED}|$SCHEMA_GENERATED|g" \
        "$FRAMEWORK_DIR/templates/CLAUDE.md.tmpl" > "$target/CLAUDE.md"

    # 4. project-config.yaml
    sed \
        -e "s|\${PROJECT_NAME}|$PROJECT_NAME|g" \
        -e "s|\${PROJECT_DESCRIPTION}|$PROJECT_DESCRIPTION|g" \
        -e "s|\${RUNTIME}|$RUNTIME|g" \
        -e "s|\${FRAMEWORK}|$FRAMEWORK|g" \
        -e "s|\${LANGUAGE}|$LANGUAGE|g" \
        -e "s|\${TEST_RUNNER}|$TEST_RUNNER|g" \
        -e "s|\${LINTER}|$LINTER|g" \
        -e "s|\${FORMATTER}|$FORMATTER|g" \
        -e "s|\${DEPLOY_CMD}|$DEPLOY_CMD|g" \
        -e "s|\${DEV_CMD}|$DEV_CMD|g" \
        -e "s|\${SCHEMA_ENABLED}|$SCHEMA_ENABLED|g" \
        -e "s|\${SCHEMA_DEFINITIONS}|$SCHEMA_DEFINITIONS|g" \
        -e "s|\${SCHEMA_GENERATED}|$SCHEMA_GENERATED|g" \
        -e "s|\${SCHEMA_GEN_CMD}|$SCHEMA_GEN_CMD|g" \
        -e "s|\${SCHEMA_CONFIG}|$SCHEMA_CONFIG|g" \
        -e "s|\${SPECS_ENABLED}|$SPECS_ENABLED|g" \
        -e "s|\${SPEC_DIRS}|$SPEC_DIRS|g" \
        -e "s|\${SPEC_PREFIX}|$SPEC_PREFIX|g" \
        -e "s|\${SOURCE_DIR}|$SOURCE_DIR|g" \
        -e "s|\${TESTS_DIR}|$TESTS_DIR|g" \
        -e "s|\${HANDOFFS_DIR}|$HANDOFFS_DIR|g" \
        -e "s|\${AGENT_COUNT}|$AGENT_COUNT|g" \
        -e "s|\${BUILDER_MODE}|$BUILDER_MODE|g" \
        -e "s|\${TYPECHECK_CMD}|$TYPECHECK_CMD|g" \
        -e "s|\${TEST_CMD}|$TEST_CMD|g" \
        -e "s|\${LINT_CMD}|$LINT_CMD|g" \
        -e "s|\${COVERAGE_THRESHOLD}|$COVERAGE_THRESHOLD|g" \
        "$FRAMEWORK_DIR/templates/project-config.yaml.tmpl" > "$claude_dir/project-config.yaml.tmp"
    sed -i "s|  \${CREDENTIALS_YAML}|  []|" "$claude_dir/project-config.yaml.tmp"
    mv "$claude_dir/project-config.yaml.tmp" "$claude_dir/project-config.yaml"

    # --- ASSERTIONS ---
    local test_errors=""

    # Check 1: No unreplaced ${...} variables in any generated file
    local unreplaced
    unreplaced=$(grep -rn '\${[A-Z_]*}' "$target" --include='*.md' --include='*.yaml' 2>/dev/null || true)
    if [ -n "$unreplaced" ]; then
        test_errors+="  UNREPLACED VARS:\n$unreplaced\n"
    fi

    # Check 2: LANG_EXT correctly substituted in project-stack.md
    local stack_globs
    stack_globs=$(grep 'globs:' -A1 "$claude_dir/rules/project-stack.md" | tail -1)
    if ! echo "$stack_globs" | grep -q "\.$expected_ext\"" ; then
        test_errors+="  LANG_EXT: expected '.$expected_ext' in globs, got: $stack_globs\n"
    fi

    # Check 3: SOURCE_DIR in project-stack.md
    if ! grep -q "\"$SOURCE_DIR/" "$claude_dir/rules/project-stack.md"; then
        test_errors+="  SOURCE_DIR: '$SOURCE_DIR' not found in project-stack.md\n"
    fi

    # Check 4: Test command correct in CLAUDE.md
    if ! grep -qF "$expected_test" "$target/CLAUDE.md"; then
        test_errors+="  TEST_CMD: expected '$expected_test' in CLAUDE.md\n"
    fi

    # Check 5: Lint command correct in CLAUDE.md
    if ! grep -qF "$expected_lint" "$target/CLAUDE.md"; then
        test_errors+="  LINT_CMD: expected '$expected_lint' in CLAUDE.md\n"
    fi

    # Check 6: project-domain.md has project name (not template var)
    if ! grep -qF "test-$test_name" "$claude_dir/rules/project-domain.md"; then
        test_errors+="  DOMAIN: project name not substituted in project-domain.md\n"
    fi

    # Check 7: project-config.yaml has correct runtime (quoted in YAML)
    if ! grep -q "runtime:.*$runtime" "$claude_dir/project-config.yaml"; then
        test_errors+="  CONFIG: runtime '$runtime' not found in project-config.yaml\n"
    fi

    # Check 8: Files are non-empty
    for f in "$target/CLAUDE.md" "$claude_dir/rules/project-stack.md" "$claude_dir/rules/project-domain.md" "$claude_dir/project-config.yaml"; do
        if [ ! -s "$f" ]; then
            test_errors+="  EMPTY: $(basename "$f") is empty\n"
        fi
    done

    # Check 9: Generated-by header present in generated files
    for f in "$claude_dir/rules/project-stack.md" "$claude_dir/rules/project-domain.md" "$target/CLAUDE.md"; do
        if ! head -1 "$f" | grep -q "Generated by Ivan's Workflow"; then
            test_errors+="  HEADER: Missing 'Generated by' in $(basename "$f")\n"
        fi
    done

    # --- Report ---
    if [ -z "$test_errors" ]; then
        echo -e "  ${GREEN}PASS${NC} $test_name ($runtime / $framework / $language → .$expected_ext)"
        PASS=$((PASS + 1))
    else
        echo -e "  ${RED}FAIL${NC} $test_name ($runtime / $framework / $language → .$expected_ext)"
        echo -e "$test_errors"
        ERRORS+="$test_name: $test_errors\n"
        FAIL=$((FAIL + 1))
    fi
}

# ── Run Tests ──────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Ivan's Workflow — Stack Installation Tests${NC}"
echo -e "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

for tc in "${TEST_CASES[@]}"; do
    IFS='|' read -r name runtime framework language ext test_cmd lint_cmd <<< "$tc"
    run_install_test "$name" "$runtime" "$framework" "$language" "$ext" "$test_cmd" "$lint_cmd"
done

echo ""
echo -e "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "  ${GREEN}PASS: $PASS${NC}  ${RED}FAIL: $FAIL${NC}"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo -e "${RED}Failures:${NC}"
    echo -e "$ERRORS"
    exit 1
fi

echo ""
echo -e "${GREEN}All stack combinations passed.${NC}"
exit 0
