# Custom Agent Rules for Workspace

This document defines specific, learned guidelines for the Antigravity agent when coding, debugging, and verifying changes in this workspace.

## 1. Systematic Debugging & Root Cause Analysis (RCA)
Before modifying any code to fix a bug (e.g., resolving issues like the NAT "Invalid class" error):
- **Mandatory 4-Phase Process**:
  1. **Replicate & Observe**: Reproduce the error and gather logs/outputs.
  2. **Trace Data Flow**: Back-trace the data flow from the point of failure to the source of data.
  3. **Analyze Race Conditions/Timing**: Verify if async operations, timing, or race conditions are causing the state discrepancy.
  4. **Formulate Fix**: Propose a fix targeting the *root cause* instead of patching the symptoms.
- **Strict Constraint**: Never write quick symptom-fixing patches. Always fix the underlying architecture/state issue.

## 2. Verification Before Completion
- **Strict Rule**: Never declare a task "done", "fixed", "passed", or "resolved" in a turn unless the actual verification command or test script has been executed and succeeded *within that same turn*.
- Always provide the execution output/logs of the test commands as proof of correctness.

## 3. Five-Axis Code Review & Quality
Before submitting code changes, perform a review across 5 axes:
1. **Correctness**: Code behavior matches requirements, edge cases handled.
2. **Readability**: Code is clean, descriptive naming, appropriate comments.
3. **Architecture**: Clean separation of concerns, appropriate design patterns.
4. **Security**: Validate inputs, sanitize data, no leaked secrets.
5. **Performance**: Verify resource usage, no unnecessary database hits or network requests.

## 4. Surgical Code Simplification (Chesterton's Fence)
- **Surgical Changes**: Only simplify or refactor code that is *directly modified* by the current task. Do not perform wide-ranging, unrelated refactoring.
- **Chesterton's Fence**: Never delete or rewrite code unless you fully understand why it was put there in the first place.
- **Alignment**: This matches Rule 8 "Surgical Changes" in `CLAUDE.md` of `c69-backend`.
