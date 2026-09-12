# 0004 - JMC #185 downstream regeneration procedure

Date: 2026-09-12. Status: accepted, consumer-specific.

One consumer repository's dated incident had accumulated in the canonical
contract. It is a runbook for a specific downstream blocker, not a Kernel gate,
and it bound no transition in this repository. Preserved here verbatim.

## Procedure, as the contract stated it

For the reported JMC #185 downstream blocker, keep review retries frozen at the
reported head until the Factory fix is independently reviewed, merged, and
released. Then use that released canonical source to regenerate the consumer's
tracked Aru integration once and reinstall hooks with
`"$ARU_SDLC_HOME/scripts/install_hooks.sh"` from the consumer checkout. Verify the
generated hook matches canonical source, run the focused semantic-drift probes
and consumer verification, and obtain one current-head approval from another account.
Release publication and consumer regeneration are separate operator work; neither
is an acceptance prerequisite for this source fix. Do not patch consumer copies
independently or relax merge-group provenance to unblock regeneration.
