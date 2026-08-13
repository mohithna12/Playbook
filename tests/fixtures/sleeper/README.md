# Recorded Sleeper responses

Trimmed captures of real Sleeper API responses, used to drive the adapter
without touching the network. Kept small and hand-checked rather than dumped
wholesale: a 5 MB player export in the repository would make every diff
unreadable and every test slow, and the fields that matter are few.

Each file is the exact JSON body Sleeper returns for the named endpoint, with
identifiers rewritten and rosters truncated. The shapes -- split `fpts` /
`fpts_decimal`, `null` inside `starters`, `adds`/`drops` keyed by player id --
are preserved exactly, because those are what the normalizer exists to handle.

`RFC R1` calls for archiving raw responses so a Sleeper schema change is
detectable. The nightly live contract test compares the real API against these
shapes; when it fails, the diff is what changed upstream.
