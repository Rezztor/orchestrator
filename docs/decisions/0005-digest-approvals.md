# 0005 Approval of a plan digest, re-planned at run time

**Status** accepted, 2026-10-25.

A change capability's call only records a dry-run plan with a digest. People approve that digest (never the person
who asked; the risk class sets how many). The run sends the digest to the executor, which plans again and refuses on
any difference, applies, verifies and rolls back where it can. Plans expire; a run happens once. Break-glass lets one
tenant administrator approve alone with a reason, outside the change window but never in a freeze or for a
destructive change.

**Why:** what people approved is exactly what runs, and an attacker cannot swap the plan between approval and run.
**Cost:** a device that changes between plan and run makes the run SUPERSEDED, and the change is proposed again.
