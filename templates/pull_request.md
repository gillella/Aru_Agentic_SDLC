## Summary

Closes #<!-- issue -->

## Verification

List only focused commands for this head: acceptance predicates, changed-path
lint/type/compile checks, directly affected tests, and invariant or secret
gates. Do not include full-suite commands. After editing this section or
pushing a new commit, rebind it with
`python3 "$ARU_SDLC_HOME/scripts/create_pr.py" --refresh-verification <PR> --body-file <file>`.

- [ ] Replace with each focused command actually run on this exact head.
- [ ] Leave only executable commands here before refresh; unchecked placeholders do not bind.

## Net surface change

Production LOC; test LOC; active docs; commands; skills; state stores:
