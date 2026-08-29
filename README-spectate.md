# Spec-quote anchors (greatspectations)

This branch anchors splicing-relevant code to the merged BOLTs spec with
verbatim, CI-checkable `/* BOLT #2: ... */` comments (tool:
rustyrussell/greatspectations, a generalization of CLN's own convention).

    uv venv .gs && uv pip install --python .gs/bin/python \
        "git+https://github.com/rustyrussell/greatspectations.git"
    git clone https://github.com/lightning/bolts.git ../bolts   # pin 1528972
    .gs/bin/greatspectate check --config specquotes.toml \
        --comment-start '/* ' --comment-continue ' *' --comment-end '*/' \
        channeld/channeld.c hsmd/libhsmd.c

Draft-spec references (`BOLT-<commit> #N:`) parse but are skipped (no
`--include-commit`); merged-spec anchors are enforced. Companion VLS-side
anchors and the REF-note convention: lightning-playground
docs/RESEARCH-VLS-SPLICE-2026-08-29.md §R5.
