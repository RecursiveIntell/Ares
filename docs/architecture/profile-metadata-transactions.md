# Existing-profile metadata transactions

`hermes_cli.profiles.update_profile_metadata` owns the participating metadata
read/check/update transaction. Description/display-name edits, generic role
references, specialist set/clear, and gateway `profiles.configure` UI metadata
all use it. It rereads `profile.yaml` under a bounded advisory lock before
checking an expected value or modifying keys. No side database or shadow epoch
is introduced. Specialist set/clear compare only their requested pointer;
`set_role_contract_ref` retains unconditional overwrite semantics, including
for `specialist-v1`. UI revisions remain per-key in the existing YAML.

The lock is a persistent adjacent file named `.<target-name>.metadata.lock`
beside the resolved metadata target. Directory aliases and `profile.yaml`
symlinks therefore serialize against the same target and preserve existing
symlink write-through behavior. Metadata replacement does not replace the lock.
Each acquisition opens a separate descriptor, including for threads. Never
remove a lock file to clear contention: waiters may still hold that inode.

POSIX uses nonblocking `flock`; Windows uses nonblocking one-byte `msvcrt`
locking. Windows can lock past EOF, so acquisition never initializes/truncates
an existing locked byte. Acquisition defaults to a five-second monotonic
deadline. OS process termination releases its lock; unavailable backends fail
explicitly. Hardlinked metadata and symlinked/hardlinked lock files are refused
because independent path identities would split transaction ownership.

Directory, original alias, target parent and lock-file identity are checked
on admission and immediately before publication. Metadata inode identity is
captured after lock acquisition and checked before publication. These checks
catch observed replacement/retargeting; they are not an adversarial filesystem
race boundary.

## Refusal and compatibility boundaries

Missing metadata starts with an empty mapping. Existing empty/null, scalar,
list, malformed YAML, malformed role/UI mappings, and malformed UI revision
values are refused without resetting or rewriting the file. In particular,
negative, boolean or non-integer revisions are not normalized back to zero.
Ordinary read/list behavior remains tolerant. Writes retain existing formatting,
permission and replacement behavior through `utils.atomic_yaml_write`.

This is deliberately an existing-object, cooperating-writer repair. It does not
serialize create/clone/import, distribution replacement, rename/delete,
external/manual editors or older writers that do not acquire this lock. A
profile path is not a stable incarnation; a display name is not an identity.
There is no global config transaction, owner-issued revision, ABA detection,
lease, enrollment boundary or cross-file transaction. The existing utility can
fall back to non-atomic copy/in-place writes, so this lock does not establish
crash-atomic publication or safe nonparticipating reads.

## Validation

The regression suite uses real spawned processes with event-based scheduling:
a first writer pauses at publication while a second demonstrably contends on
the lock. It covers same-expected CAS, clear/set, disjoint description/role/UI
updates, directory/file aliases, replacement, timeout, process death, malformed
metadata refusal and thread contention. Native macOS/Windows marked tests are
selected by the existing OS CI workflow. Linux results alone do not certify
those other lock backends.
