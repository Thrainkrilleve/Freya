# General context system

This implements the first local-alpha layer of the Context Authority described
in section 8 of `docs/specs/Freya_Foundation_v1.1_baseline.docx`. The baseline is
authoritative; the Claude amendments remain proposals and ADR-005 remains pending.
This is implementation progress, not architectural approval or an acceptance claim.

## How context is assembled

`kernel/context/authority.py` owns scope checks, ranked source selection, input
accounting and complete recent dialogue exchanges. `core/assistant.py` uses the
same preparation path for ordinary and streaming replies. Model calls continue
through the existing capability broker.

The trusted system instructions are separate from retrieved records. Memory,
conversation summaries, recalled passages and media analysis enter as labelled
data packets with source IDs, trust, owner, domain and permitted uses. Tool results
retain native tool-call structure and carry untrusted-result metadata. These
boundaries support CTX-001; they do not make model prompt injection impossible.

Candidate memory scope is checked before model reranking. The current alpha uses
the existing fixed `local-owner` principal; legacy records without scope fields
are treated as that owner's local records and marked legacy-unverified. This is
not authenticated multi-user isolation or full MEM-003/MEM-007 compliance.

Cached conversation summaries are refreshed from canonical records for each
request. Deleted records are excluded. The original stored conversations remain
intact when their prompt view is shortened (MEM-005). This does not implement
complete derived-data tombstones or all deletion requirements in MEM-008.

Long summary inputs are processed in bounded chunks in order. A failed or empty
chunk leaves the saved summary cursor unchanged, so earlier turns are not silently
marked summarized after only the transcript tail was supplied to the model.

## Budgets and inspection

The defaults are a 16,384-token window, a 2,048-token output reserve and 512 tokens
of overhead. Set `FREYA_CONTEXT_WINDOW_TOKENS` and
`FREYA_CONTEXT_OUTPUT_TOKENS` in the launching environment to override them.
The reply calls explicitly send Ollama `num_ctx` and `num_predict`.

Accounting deliberately charges each UTF-8 byte as a token, plus message overhead;
it is a conservative estimate, not the model's exact tokenizer. Tool definitions
are counted. Source data receives at most one third of the input budget, each
source/tool text is bounded, and truncation is labelled. The current user request
is preserved; requests whose pinned content exceeds the budget return a clear
error. Older complete dialogue exchanges are omitted first as tool rounds grow.
The authority supports an injected tokenizer for a future provider implementation.

Use `/context` after a reply to inspect source IDs, trust labels, estimated input
use, omissions and retrieval status. It does not print retrieved text or the user
query. Retrieval failures and unavailable sources must not imply that Freya has
never encountered the subject.

Larger Ollama context windows consume more memory; defaults depend on hardware.
See [Ollama context length](https://docs.ollama.com/context-length) and
[model parameters](https://docs.ollama.com/modelfile). Auxiliary memory/reranking
calls do not yet share this reply budget or capacity admission.

## Remaining specification work

Authenticated task/session scope, consequential-action goal binding (CTX-002),
full record lineage and permitted-use enforcement across all stores, derived-data
deletion, exact tokenization, provider-capacity admission, asynchronous compaction
and additional knowledge/document providers remain pending. No storage migrations,
policy rules or owner data were changed as part of this work.

Tests use synthetic temporary stores and fake model responses. Live model quality,
hardware capacity and adversarial model behaviour need separate owner-run checks.
