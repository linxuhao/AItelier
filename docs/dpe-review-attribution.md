# Current producer scope during DPE review

A normal claimed `t_impl_review` can come from an already pinned generated graph
that supplies the current item and producer outputs but no task card. Its missing
card correctly denies repository writes; that review write policy is separate
from the scope under which the completed `t_impl` produced its code.

Closeout binds the live reviewer run/item/claim and latest completed producer,
then reads only that exact producer instance's persisted claim context. Its valid
structured task card must match the item and the write-scope receipt's allowed
scope and final producing claim. A declared reviewer card, when present, must
agree. Missing/corrupt producer authority or mismatched identities still refuse;
a writable receipt alone cannot grant scope. No all-run payload fetch is needed.

The full committed diff and recorded attempted out-of-scope operations still
reach review as actionable closeout findings. A scope violation is not relabeled
as an identity mismatch or erased. The reviewer receives no producer write grant.

`tests/integration/test_dpe_claimed_review_scope.py` uses actual SDK loop claims,
normal resolved contexts, host runner/PipelineEngine, real refusal/code receipts
and SDK commit confirmation; only the paid completion transport is scripted.
Its positive claim context is never patched to add the missing card. Separate
controls corrupt one retained authority/identity at a time and must refuse before
review completion. Author execution is UNRUN; Source acceptance, deployment and
recovery of an earlier failed run remain separate.
