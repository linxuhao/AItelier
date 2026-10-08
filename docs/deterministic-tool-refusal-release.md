# Writing Bench definitive replay refusal

The current host source exact-pins `skillflow-py==1.5.87`. The public
`ToolExecutionRefused` contract introduced in 1.5.85 continues to support the
independently reviewed Writing Bench SOURCE change.
Only the existing full-replay warnings/drift refusal raises `BenchReplayRefused`;
the validator and original reason are unchanged. Other `BenchError` and I/O
failures retain their existing retries.

Reviewed host SOURCE: `01555fc63c35dc9bf909c6436151bc19359a95c0`.
Reviewed SDK SOURCE: `10b5cfa5bb5a14e78ed9b13722786a2b533b84a8`.
The 1.5.85 release preparation changed only the SDK version, the host pin, and
release documentation. The earlier source-requirement receipt remains historical.

Publication and normal runtime activation must use the separately recorded
exact release artifacts and deployment evidence. Changing this pin or building
packages does not establish installed or loaded runtime behavior.
