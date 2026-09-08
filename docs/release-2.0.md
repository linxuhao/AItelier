# AItelier 2.0 release preparation

Status: preparation, not a published release (2026-09-08).

## Release scope

The major change is persistent project State DAGs, separate from workflow
execution: revisioned goals and criteria, dependencies and invalidation,
workflow and external-harness attempts, per-criterion evidence, explicit
verification, a project-first dashboard, durable change cursors and reusable
MCP driver guidance. Completion produces a candidate, never automatic acceptance.
Existing legacy tasks are imported explicitly; history is not promoted to verified.

## Evidence available

The State wait/guide delivery at d319cd5 passed 283 selected Python tests,
including State, MCP routing and scheduler integration. The restarted production
service exposed the new help/guide and returned an existing durable event through
the wait action. These are scoped checks, not a full release certification.

## Required before tagging 2.0.0

- Run the full offline Python suite and frontend tests/build on one frozen release
  commit. Preserve failures and distinguish network-dependent tests.
- Rehearse upgrade against a copied database and workspace; preserve legacy run
  references and verify State privacy, external evidence, checkpoint recovery and
  restart/cursor behavior. Back up live data before deployment.
- Check the existing npm client package `integrations/dsh` (`dsh-plugin-aitelier`,
  source version 0.1.8): refresh its bundled skill and README for State DAG,
  private State read authorization and live driver-guide discovery; inspect
  `npm pack` contents and verify installation against the accepted backend.
- PyPI distribution is not a requirement for this release. Source Python metadata
  alone does not establish an existing PyPI release. If added later, separately
  validate wheel/sdist assets and clean installation outside the source tree.
- Build a clean Docker image and smoke-test it with the built frontend and the
  declared official SkillFlow dependency, without editable host dependencies.
- Align release identifiers: Python metadata currently says 1.0.0; the private
  frontend package says 2.0.0. Check API-reported versions too. Change the release
  version once the chosen candidate and distribution checks are ready.
- Prepare release notes, supported installation instructions, migration/backup
  steps and known limitations. Check the intended public tree for credentials,
  private generated configs and project content before upload.

## Publication surfaces

- Git repository: reviewed source/docs, version tag and GitHub release notes.
- npm: publish the updated `dsh-plugin-aitelier` client bundle after its package
  and backend compatibility checks. Its version can evolve independently of the
  backend major version.
- Container registry: optional versioned image if binary container distribution is
  supported; otherwise document building from the tagged checkout.
- Hosted service: deploy the accepted version and verify HTTP, MCP and frontend.
  A service restart is not a public software release.
- SkillFlow: no separate engine release is required for these AItelier-only State
  changes; use the existing declared official dependency.
- Frontend: `aitelier-web` is private; no separate npm publication. Its build is
  served by the application. MCP prompts/resources ship with the server.

Public publication requires an explicit release decision after reviewing the
concrete candidate and evidence. No game repository, design, private State DB,
workspace artifacts or secrets belong in the AItelier release.
