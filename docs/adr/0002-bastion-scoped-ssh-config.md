# `ssh_config: "default"` means "the Bastion's SSH config", not "SSH-default.json"

Different Bastions need different credentials, and every node behind a Bastion
is reached with that Bastion's key. We redefined the request value
`ssh_config: "default"` (also the value when the field is omitted) from "load
`SSH-default.json`" to **auto-select**: for `bastion`, `cluster` and `hostname`
host types, look up the Bastion fronting the target and load
`SSH-<bastion-name>.json`, falling back to `SSH-default.json` only when that
file does not exist. `ip` hosts have no Bastion and keep the old behaviour.
Existing callers get the new behaviour without changing anything, which is the
point.

## Considered options

**A new sentinel (`"auto"`, or `None`) leaving `"default"` literal** was
rejected because it would require every existing caller to opt in. The price of
the chosen option is that a caller can no longer *force* the Fallback SSH config
for a host that has a Bastion; if that is ever needed, the way out is to remove
the Bastion's file, not to ask for `"default"`.

**`hostname` keeps using `SSH-default.json`** looked natural — its SSH target is
the node itself, not the Bastion — but is wrong for our estate: nodes accept the
Bastion's key. So `hostname` pays one extra inventory call (bastion mapping
lookup) whenever it auto-selects. When the caller names an SSH config
explicitly, no Bastion lookup happens at all.

**Silent fallback for an explicitly named SSH config** (the old behaviour) was
dropped. Now that `"default"` carries the fallback, an explicit name expresses
intent, and silently authenticating with different credentials hides a missing
secret. An explicit name with no file is a 400.

## Consequences

- A `hostname` + `"default"` request whose node has no Bastion mapping (unmapped
  `node_type`, no matching pattern, or inventory failure) now **fails** where it
  used to succeed. This is deliberate: the fix is to complete the mappings in
  the inventory API, not to fall back.
- The Run records its **Effective SSH config** (the name actually loaded,
  `default` if it fell back), and kill/trace/poll load that name literally —
  they never re-run auto-selection. Otherwise a kill on another pod would depend
  on the inventory being up and unchanged, and could authenticate with a
  different key than the launch did. A Run whose recorded SSH config no longer
  exists gets a 400 naming the missing config on kill / trace — never a silent
  fallback — and the kill is refused *before* the Run moves to KILLING so it is
  not stranded. This includes Runs launched before this change with an explicit
  name that only worked via the old silent fallback; Runs that recorded
  `"default"` load exactly as before.
- A Bastion name comes from an external API and becomes part of a file path in
  a directory of private keys, so it is validated against a strict pattern and a
  bad one is a 500 (upstream data error). A caller-supplied `ssh_config` gets the
  same check and a bad one is a 422.
- Name resolution and validation run in dry-run mode too; only the file read is
  replaced (see `docs/arch/dry-run-mode.md`).
