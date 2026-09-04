# Memory Quality Phase 5.9 — consumer delivery result

Status: **COMPLETE RUN — FORMAL RULING REMAINS INSUFFICIENT**

Frozen run: `b5ce01e4-c156-4fa5-a7d1-d01e3527512f`  
Corpus: `delivery-v2` (`98896e32a00b6d048fa83da8acd5bf7ccbd56f30470e9074ed29a80bf2411383`)  
Policy: `delivery-policy-v2` (`e67903a948c65814dc4df3fe2c5f2f43e742b17b7babaebeb9931eceb0ca6307`)  
Matrix: 10 cases x 5 arms x 3 repetitions = 150 observations.

The experiment seeded synthetic gold state directly. It did not run transcript extraction or
scope routing. ACH arms used the production structured-profile compiler and INDEX/FULL composers;
the official arms used fresh Hindsight 0.9.2 banks; every answer came from the same isolated,
no-tools headless consumer.

## Aggregate result

| Arm | All gates | Context complete | Consumer valid | Median total | p95 total | Named failing cases |
|---|---:|---:|---:|---:|---:|---|
| ACH INDEX | 24/30 | 27/30 | 27/30 | 608 ms | 4,577 ms | D09, D10 |
| ACH FULL | 24/30 | 27/30 | 27/30 | 590.5 ms | 814 ms | D09, D10 |
| Official reflect | 20/30 | 27/30 | 20/30 | 12,712.5 ms | 25,212 ms | D03, D04, D05, D10 |
| Official Pages | 1/30 | 1/30 | 28/30 | 23,048.5 ms | 27,859 ms | D01-D10 |
| Hybrid | 6/30 | 9/30 | 27/30 | 23,879.5 ms | 28,276 ms | D02, D04-D10 |

`consumer_valid` credits a correct abstention when the delivered context lacks the required unit;
it must not be read as content recall. `context complete` is the direct delivery measure.

## Findings

1. **The structured ACH profile is the strongest measured startup baseline.** INDEX and FULL
   delivered every expected startup item in D01-D08 in all repetitions. D07 proves that the
   risk-ranked negative rule survives a 26-item project profile and the 25-item compiler cap.
2. **Reflect adds a distinct long-tail capability.** It alone delivered D09's history-only
   rationale in all repetitions. It is not a safe replacement for the startup profile: it surfaced
   the superseded D05 choice in all repetitions, lost D10 and produced worse consumer answers on
   D03/D04. Median total latency was about 21 times ACH FULL.
3. **The measured Page configuration is not a profile replacement.** Most generated pages said
   the retrieved data was insufficient; only one D07 repetition carried its required unit. The
   hybrid consequently delivered only its deterministic User Core, Project Metadata and Working
   State cases; it did not recover the project profile or long-tail rationale.
4. **INDEX and FULL were behaviorally indistinguishable in this corpus.** Both have the same
   per-case content and consumer outcomes. This run supplies no positive evidence that two copies
   plus a revision protocol are required. It also did not exercise a real stale-cache failover, so
   D10 cannot validate the distributed INDEX/FULL consistency protocol.
5. **D10's automatic consumer gate is overly literal.** The ACH consumer correctly answered that
   the context was 3,600 seconds old and repeated `0000003600s`, but the rubric required the exact
   phrase `cache-age 0000003600s`. The frozen score is preserved as failed; the semantic answer is
   recorded as a corpus-oracle limitation, not silently rescored.
6. Two of 150 consumer subprocesses failed closed, both on Official Pages (D01 and D09). Their
   delivered contexts remain frozen, but the policy requires a complete consumer matrix. Therefore
   `consumer_complete=false` and no formal production/adoption decision is emitted from this run.

### Post-run consumer diagnostic

The two frozen contexts that lacked consumer output were replayed outside the scored matrix on
2026-09-03, without regenerating Pages or changing any frozen artifact. D01 and D09 each completed
three of three sequential consumer calls successfully (D01 about 1.4 s; D09 about 1.4--4.0 s).
All six returned the expected valid abstention because neither Page contained its required answer.
This isolates the original two non-zero subprocess exits as transient consumer execution failures,
not Page-generation timeouts. It does not retroactively make the frozen matrix complete and does
not improve the Pages arm's content score.

## Formal and directional rulings

```text
profile_compiler = insufficient_evidence        # formal policy gate
ranking_displacement = insufficient_evidence    # no internal ablation; one pressure case only
index_full = insufficient_evidence               # identical measured behavior
delivery_protocol = insufficient_evidence        # no real cache/revision failure exercise
consumer_complete = false                        # 148/150 valid outputs
```

The direction is nonetheless strong enough to constrain the next design:

- keep the ACH structured compiler as the delivery baseline while production remains off;
- keep low-budget reflect as explicit long-tail retrieval, not routine startup injection;
- reject the measured Pages-only and Page-backed hybrid as replacements;
- do not expand INDEX/FULL or its consistency protocol without a dedicated single-channel/cache
  ablation;
- do not claim ranking/displacement generally necessary until its removal is measured on at least
  two independent pressure cases.

## Safety and artifacts

- All 90 disposable banks were deleted; no run-prefixed bank remains.
- No declared canary, bank ID or credential appears in the artifacts.
- Production activation flags remained off and Phase 0 was not executed.
- `manifest.json`: `2a2c8185a3cfc8672562a3c7d2e45f780148764e1f93628ec38be0efdce2e54a`
- `observations.jsonl`: `b23c19529f115fce467283cf00632f80397998d4dffafbb9eac8464a88531c4a`
- `summary.json`: `2ce4622ef52d0281b0fdbf991390212e3439313905499cdf83609dfad5ad10e7`
