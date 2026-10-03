# Security boundaries

## Identity and routing

Every external Service operation requires a trusted `Context`. Ownership checks
always include principal and profile. Admin grants do not bypass another
principal's task ownership. Create separately requires task:create,
executor:use:<id>, client:use:<id>, and transport:use:<id>. Read/reply/cancel/list
use their own grants. Client JSON cannot set principal, profile, scopes or
approval flags. Routing identifiers are bounded and require explicit grants.

A local CLI/stdio process is an extension of its OS user. Do not expose that
local owner's context over an unauthenticated network. Remote transport adapters
must establish identity independently. This milestone does not implement remote
credential provisioning, OAuth, token storage or an Internet-facing listener.

## Untrusted data and effects

Inputs, references, replies and outputs remain data. Mock echoes data and has no
external effects. `approved: true` inside input/reply is inert; it never authorizes
an operation. Unknown capabilities default false. No real executor is enabled by default. Explicit local --command-config can
register an operator-trusted fixed-command Executor; never select that file from
untrusted task data. See adapters.md for its POSIX process-group limitations.
Task results are delivery records, never new incoming tasks; loop-origin and hop
checks provide additional defenses.

Executor callbacks and delivery acknowledgements are internal trusted adapter
interfaces. They are fenced to claimed work, but claim IDs are not a network
authentication scheme. Never expose those methods directly to untrusted clients.

## Recovery and duplicate limits

Same scoped request/event ID plus same payload returns the same task. Reusing an
ID with different content is a conflict. Concurrent workers cannot claim the
same pending dispatch. This is not a guarantee of exactly-once effects in a
remote system. Executors remain responsible for their own idempotency and effect
policy. After an ambiguous execution or delivery, automated replay is forbidden.
Only an explicit future reconciliation feature may resolve such uncertainty.

Cancellation of queued work prevents dispatch. Cancellation of running work is
only a request; a successful result may still arrive. Timeout or adapter failure
can leave an unknown effect outcome, recorded as result_uncertain.

## Secrets and retention

Core stores no authentication credentials and never passes process environment
to executors. Exception text from executors is not persisted. There is no general
secret detector: user-provided task content can itself contain a secret and will
be persisted or echoed. Adapters/operators must minimize and redact sensitive
input. Do not send tokens, credentials, browser cookies or other products'
internal databases through tasks. Do not log raw payloads or credentials.

New database and backup files use owner-only creation permissions. Protect the
containing volume, backups and host account; existing paths and Windows ACLs
must be checked by operators. Encryption at rest and retention deletion are not
implemented. Keep the private data volume outside Git and distribute only source.

## Deployment and dependencies

A non-root, no-network local Docker/Compose candidate is included. It defaults to
Mock only; production readiness, remote authentication and secret provisioning
are not established. Use effect-free fixtures until each real integration is
explicitly authorized. Public ingress, scopes, OAuth, credentials, registry publish,
production posts and deployment require separate authorization. Apache-2.0 covers
original project code. No source from MANY-AI-CLI or Deskly is imported.
