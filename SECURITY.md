# Security reports

Send suspected vulnerabilities to founders@kimpton.ai with the subject `EnvironmentHarness security`. Include the affected version, reproduction and impact. Do not publish credentials, private trajectories or a working exploit in a public issue. This project does not promise a response deadline or bug bounty.

The local Python API trusts code with access to its store. A command subprocess is not a security sandbox. Participant isolation is enforced by authenticated HTTP requests and scoped credentials; the researcher's viewer filters are not a security boundary. Use isolated backends for untrusted programs and keep service credentials outside source control.
