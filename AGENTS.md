# Project agent instructions

## Completion review

After making code changes and before declaring a task complete, invoke the **`ponytail-review`** skill on the resulting diff to find over-engineering, bloat, and unnecessary complexity. Apply its simplification suggestions unless they directly conflict with explicit requirements, tests, or project constraints.

For branch- or project-level work, also run **`ponytail-audit`** on the whole repository before merging or opening a pull request.
