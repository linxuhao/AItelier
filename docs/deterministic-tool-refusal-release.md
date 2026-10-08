# Writing Bench definitive replay refusal

The host exact-pins `skillflow-py==1.5.85`, whose public `ToolExecutionRefused`
contract supports the independently reviewed Writing Bench SOURCE change.
Only the existing full-replay warnings/drift refusal raises `BenchReplayRefused`;
the validator and original reason are unchanged. Other `BenchError` and I/O
failures retain their existing retries.

Reviewed host SOURCE: `01555fc63c35dc9bf909c6436151bc19359a95c0`.
Reviewed SDK SOURCE: `10b5cfa5bb5a14e78ed9b13722786a2b533b84a8`.
The release preparation changes only the SDK version, this host pin, and release
documentation. The earlier source-requirement receipt remains historical.

Publication and normal runtime activation must use the separately recorded
exact release artifacts and deployment evidence. Changing this pin or building
packages does not establish installed or loaded runtime behavior.
