#!/bin/bash
set -euo pipefail

PLAN_FILE="PLAN.md"
TASK_COUNTER=0
SLEEP_SECONDS=10
TASK_TIMEOUT=600
OPENCODE_ARGS=(--agent OpenCoder -m "zai-coding-plan/glm-5.1" --dangerously-skip-permissions --print-logs)
OPENCODE_ARGS=(--agent OpenCoder -m "zai-coding-plan/glm-5.1" --dangerously-skip-permissions)

log() {
    echo "[$(date '+%H:%M:%S')] $*"
}

separator() {
    echo "─────────────────────────────────────────────────────"
}

touch "$PLAN_FILE"
log "🚀 h24loop started — watching $PLAN_FILE"

while true; do
    separator

    if ! grep -q "\[ \]" "$PLAN_FILE"; then
        log "🔍 No pending tasks found. Generating new ones..."

        timeout "$TASK_TIMEOUT" opencode run "${OPENCODE_ARGS[@]}" "
            You are a senior technical lead project owner, engineer and CEO. Review the current codebase.
            Identify several meaningful improvements (e.g., refactoring, performance optimization, error handling, or test coverage). 
            Append these tasks to PLAN.md using '- [ ] Task description' syntax.
            Do not write any implementation code, ONLY update PLAN.md.
        " || log "⚠️ Task generation timed out after ${TASK_TIMEOUT}s"

        log "📝 New tasks generated. Restarting loop..."
        continue
    fi

    NEXT_TASK=$(grep "\[ \]" "$PLAN_FILE" | head -1)
    log "⚡ Executing task #$((TASK_COUNTER + 1)): $NEXT_TASK"

    timeout "$TASK_TIMEOUT" opencode run "${OPENCODE_ARGS[@]}" "
        1. Read PLAN.md.
        2. Pick the FIRST uncompleted '- [ ]' task in PLAN.md.
        3. Implement the improvement in the codebase.
        4. Mark the task as done in PLAN.md by changing '- [ ]' to '- [x]'.
        5. CRITICAL: Commit the task when completed, dont ask for permission.
        ONLY DO ONE TASK. DO NOT proceed to the next task.
    " || log "⚠️ Task execution timed out after ${TASK_TIMEOUT}s"

    TASK_COUNTER=$((TASK_COUNTER + 1))
    log "✅ Task #$TASK_COUNTER completed."

    if (( TASK_COUNTER % 10 == 0 )); then
        log "🧹 10-task milestone! Running context harvest..."

        timeout "$TASK_TIMEOUT" opencode run "${OPENCODE_ARGS[@]}" "
            Execute autonomously the following two commands, in this order, without asking for user permission,
            make the decision yourself and run untill completion, then commit the changes with a message starting with 'openagentcontrol context update':
            1. /context harvest
            2. /add-context --update
        " || true

        log "✨ Context updated."
    fi

    log "💤 Sleeping ${SLEEP_SECONDS}s before next iteration..."
    sleep "$SLEEP_SECONDS"
done